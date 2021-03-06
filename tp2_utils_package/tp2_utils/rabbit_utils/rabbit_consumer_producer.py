import json
import logging
from functools import partial
from typing import Callable, NoReturn, Optional, Dict, List, Tuple

import pika

from tp2_utils.rabbit_utils.publisher_sharding import PublisherSharding
from tp2_utils.rabbit_utils.special_messages import BroadcastMessage
from .message_set.message_set import MessageSet

PUBLISH_SHARDING_FORMAT = "%s_shard%d"


class RabbitQueueConsumerProducer:
    """
    Rabbit consumer-producer
    """
    logger: logging.Logger = None

    def consume(self, consume_func: Callable[[Dict], Tuple[List, bool]],
                ch, method, properties, body) -> NoReturn:
        stop_consumer = False
        responses = []
        messages = []
        try:
            data = json.loads(body)
            if isinstance(data, list):
                for message in data:
                    messages.append(message)
                    if not self.idempotency_set or self._obj_to_bytes(message).encode() not in self.idempotency_set:
                        resp, _stop = consume_func(message)
                        stop_consumer = _stop or stop_consumer
                        if resp:
                            for r in resp:
                                responses.append(r)
            else:
                if not self.idempotency_set or self._obj_to_bytes(data).encode() not in self.idempotency_set:
                    messages.append(data)
                    resp, _stop = consume_func(data)
                    stop_consumer = _stop or stop_consumer
                    if resp:
                        for r in resp:
                            responses.append(r)
        except Exception:
            if RabbitQueueConsumerProducer.logger:
                RabbitQueueConsumerProducer.logger.exception("Exception while consuming message")
            ch.basic_nack(delivery_tag=method.delivery_tag)
            return
        try:
            if responses:
                broadcast_messages = []
                buffer = []
                for response in responses:
                    if isinstance(response, BroadcastMessage):
                        broadcast_messages.append(response)
                        continue
                    if self.messages_to_group == 1:
                        self._publish(ch, response)
                    else:
                        buffer.append(response)
                        if len(buffer) == self.messages_to_group:
                            self._publish(ch, buffer)
                            buffer = []
                if buffer:
                    self._publish(ch, buffer)
                if broadcast_messages:
                    for message in broadcast_messages:
                        self._publish(ch, message)
        except Exception:
            if RabbitQueueConsumerProducer.logger:
                RabbitQueueConsumerProducer.logger.exception("Exception while sending message")
            ch.basic_nack(delivery_tag=method.delivery_tag)
            return
        if self.idempotency_set:
            for message in messages:
                if message:
                    self.idempotency_set.add(self._obj_to_bytes(message).encode())
        ch.basic_ack(delivery_tag=method.delivery_tag)
        if stop_consumer:
            ch.stop_consuming()
            self.connection.close()

    def _obj_to_bytes(self, obj):
        if isinstance(obj, BroadcastMessage):
            return json.dumps(obj.item)
        elif isinstance(obj, list):
            return json.dumps([elem for elem in obj if elem])
        else:
            return json.dumps(obj)

    def _publish(self, channel, obj):
        if self.publisher_sharding:
            if isinstance(obj, list):
                shard_lists = {}
                for item in obj:
                    shards = self.publisher_sharding.get_shards(item)
                    for shard in shards:
                        if shard not in shard_lists:
                            shard_lists[shard] = [item]
                        else:
                            shard_lists[shard].append(item)
                for k, v in shard_lists.items():
                    for queue in self.response_queues:
                        resp_queue = PUBLISH_SHARDING_FORMAT % (queue, k)
                        channel.basic_publish(exchange='', routing_key=resp_queue,
                                              body=self._obj_to_bytes(v))
            else:
                for queue in self.response_queues:
                    shards = self.publisher_sharding.get_shards(obj)
                    for shard in shards:
                        resp_queue = PUBLISH_SHARDING_FORMAT % (queue, shard)
                        channel.basic_publish(exchange='', routing_key=resp_queue,
                                              body=self._obj_to_bytes(obj))
        else:
            for queue in self.response_queues:
                channel.basic_publish(exchange='', routing_key=queue,
                                      body=self._obj_to_bytes(obj))

    def __init__(self, host: str, consume_queue: str,
                 response_queues: List[str],
                 consume_func: Callable[[Dict], Tuple[List, bool]],
                 messages_to_group: int = 1,
                 idempotency_set: Optional[MessageSet] = None,
                 publisher_sharding: Optional[PublisherSharding] = None,
                 logger: Optional[logging.Logger] = None):
        """
        Start a rabbit consumer-producer

        :param host: the hostname to connect
        :param consume_queue: the name of the queue to consume
        :param response_queues: the name of the queue in which the response will be sent
        :param consume_func: the function that consumes and creates a response,
                receives a dict and return a list of dicts to respond and a boolean
                indicating whether to stop consuming
        :param messages_to_group: the amount of messages to group
        :param idempotency_set: an object of type DMessageSet to handle the arrival
        of duplicated messages
        :param publisher_sharding: object for sharding messages
        :param logger: the logger to use
        """
        if logger:
            RabbitQueueConsumerProducer.logger = logger
        self.idempotency_set = idempotency_set
        self.consume_queue = consume_queue
        self.response_queues = response_queues
        self.messages_to_group = messages_to_group
        self.connection = pika.BlockingConnection(pika.ConnectionParameters(host=host))
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue=consume_queue)
        for resp_queue in response_queues:
            if publisher_sharding:
                for shard in publisher_sharding.get_possible_shards():
                    self.channel.queue_declare(queue=PUBLISH_SHARDING_FORMAT % (resp_queue, shard))
            else:
                self.channel.queue_declare(queue=resp_queue)
        consume_func = partial(self.consume, consume_func)
        self.channel.basic_consume(queue=consume_queue,
                                   on_message_callback=consume_func,
                                   auto_ack=False)
        self.publisher_sharding = publisher_sharding

    def __call__(self, *args, **kwargs):
        self.channel.start_consuming()
