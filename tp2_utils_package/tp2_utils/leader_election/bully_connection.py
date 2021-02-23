import socket

from multiprocessing import Process, Queue
from typing import Dict, Tuple
from time import sleep

from tp2_utils_package.tp2_utils.json_utils.json_receiver import JsonReceiver
from tp2_utils_package.tp2_utils.json_utils.json_sender import JsonSender
from tp2_utils_package.tp2_utils.leader_election.bully_leader_election import BullyLeaderElection
from tp2_utils_package.tp2_utils.leader_election.replica_behaviour import ReplicaBehaviour

LISTEN_BACKLOG = 5
CONNECTION_LAYER = "CONNECTION"
BULLY_LAYER = "BULLY"
ACK_MESSAGE = "ACK"

SOCKET_TIMEOUT = 2


class BullyConnection:
    @staticmethod
    def _open_sending_socket_connection(host, port):
        connection = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
        return connection

    def _generate_ack_message(self):
        return {
            "layer": CONNECTION_LAYER,
            "message": ACK_MESSAGE,
            "host_id": self._host_id
        }

    def _generate_bully_message(self, message):
        return {
            "layer": BULLY_LAYER,
            "message": message,
            "host_id": self._host_id
        }

    # TODO: Situation: a process send an election message but doesn't get a response, because the other just receives.

    def launch_listening_process(self, port, incoming_messages_queue, outcoming_messages_queue):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('', port))
        sock.listen(LISTEN_BACKLOG)
        connection, address = sock.accept()
        while True:
            message = JsonReceiver.receive_json(connection)
            if message["layer"] == CONNECTION_LAYER:
                JsonSender.send_json(connection, self._generate_ack_message())

            elif message["layer"] == BULLY_LAYER:
                incoming_messages_queue.put(message["message"])
                JsonSender.send_json(connection, outcoming_messages_queue.get())

    def __init__(self, bully_connections_config: Dict[int, Tuple], lowest_port: int, host_id: int):
        """
        Initializes connections and bully
        :param bully_connections_config: Dictionary that contains numerical ids of hosts as keys
        and a tuple with the host ip and port that will have a listening socket to receive messages as values.
        :param lowest_port: Integer that represents the lowest port to listen from other nodes.
        :param host_id: Numerical value that represents the id of the host for the bully algorithm.
        """
        self._bully_connections_config = bully_connections_config
        self._host_id = host_id
        self._sockets_to_send_messages = {}
        self._bully_messages_queue = Queue()
        self._bully_response_messages_queues = {}

        for h_id in range(len(bully_connections_config)):
            bully_response_messages_queue = Queue()
            self._bully_response_messages_queues[h_id] = bully_response_messages_queue
            listening_process = Process(target=self.launch_listening_process,
                                        args=(lowest_port, self._bully_messages_queue,
                                              self._bully_response_messages_queues[h_id]))
            listening_process.start()
            lowest_port += 1

        self._sending_connections = {}

        for h_id, host_and_port in bully_connections_config.values():
            self._sending_connections[h_id] = self._open_sending_socket_connection(host_and_port[0], host_and_port[1])

        self._bully_leader_election = BullyLeaderElection(host_id, list(bully_connections_config.keys()) + [host_id])

    def _run(self):
        """
        Loops doing the connections tasks. Requests ACK from leader node and check if it has received any other messages.
        """
        while True:
            current_leader = self._bully_leader_election.current_leader()
            if current_leader != -1 or current_leader != self._host_id:
                replica_behaviour = ReplicaBehaviour(self._sending_connections, self._host_id, current_leader, self._bully_leader_election)
                replica_behaviour.execute_tasks()
            while not self._bully_messages_queue.empty():
                received_message = self._bully_messages_queue.get()
                message = self._bully_leader_election.receive_message(received_message)
                if message:
                    self._bully_response_messages_queues[received_message["host_id"]].put(message)
                else:
                    self._bully_response_messages_queues[received_message["host_id"]].put(self._generate_ack_message())
            sleep(3)

    def current_leader(self):
        """
        Return current identifying number leader.
        """
        return self._bully_leader_election.current_leader()

    def start(self):
        self._send_election_messages(retry_connection=True)
        self._run()
