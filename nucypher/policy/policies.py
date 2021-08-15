"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""


import math
from abc import ABC, abstractmethod
from typing import Tuple, Sequence, Optional, Iterable, List, Dict, Type

import maya
from bytestring_splitter import BytestringSplitter, VariableLengthBytestring
from eth_typing.evm import ChecksumAddress
from twisted.internet import reactor

from nucypher.blockchain.eth.constants import POLICY_ID_LENGTH
from nucypher.crypto.kits import RevocationKit
from nucypher.crypto.powers import TransactingPower, DecryptingPower
from nucypher.crypto.splitters import key_splitter
from nucypher.crypto.utils import keccak_digest
from nucypher.crypto.umbral_adapter import PublicKey, VerifiedKeyFrag, Signature
from nucypher.crypto.utils import construct_policy_id
from nucypher.network.middleware import RestMiddleware
from nucypher.policy.maps import TreasureMap
from nucypher.policy.reservoir import (
    make_federated_staker_reservoir,
    MergedReservoir,
    PrefetchStrategy,
    make_decentralized_staker_reservoir
)
from nucypher.utilities.concurrency import WorkerPool, AllAtOnceFactory
from nucypher.utilities.logging import Logger


class Arrangement:
    """
    A contract between Alice and a single Ursula.
    """

    splitter = BytestringSplitter(
        key_splitter,                      # publisher_verifying_key
        (bytes, VariableLengthBytestring)  # expiration
    )

    def __init__(self, publisher_verifying_key: PublicKey, expiration: maya.MayaDT):
        self.expiration = expiration
        self.publisher_verifying_key = publisher_verifying_key

    def __bytes__(self):
        return bytes(self.publisher_verifying_key) + bytes(VariableLengthBytestring(self.expiration.iso8601().encode()))

    @classmethod
    def from_publisher(cls, publisher: 'Alice', expiration: maya.MayaDT) -> 'Arrangement':
        publisher_verifying_key = publisher.stamp.as_umbral_pubkey()
        return cls(publisher_verifying_key=publisher_verifying_key, expiration=expiration)

    @classmethod
    def from_bytes(cls, arrangement_as_bytes: bytes) -> 'Arrangement':
        publisher_verifying_key, expiration_bytes = cls.splitter(arrangement_as_bytes)
        expiration = maya.MayaDT.from_iso8601(iso8601_string=expiration_bytes.decode())
        return cls(publisher_verifying_key=publisher_verifying_key, expiration=expiration)

    def __repr__(self):
        return f"Arrangement(publisher={self.publisher_verifying_key})"


class TreasureMapPublisher:

    log = Logger('TreasureMapPublisher')

    def __init__(self,
                 treasure_map_bytes: bytes,
                 nodes: Sequence['Ursula'],
                 network_middleware: RestMiddleware,
                 percent_to_complete_before_release: int = 5,
                 threadpool_size: int = 120,
                 timeout: float = 20):

        self._total = len(nodes)
        self._block_until_this_many_are_complete = math.ceil(len(nodes) * percent_to_complete_before_release / 100)

        def put_treasure_map_on_node(node: 'Ursula'):
            try:
                response = network_middleware.put_treasure_map_on_node(node=node,
                                                                       map_payload=treasure_map_bytes)
            except Exception as e:
                self.log.warn(f"Putting treasure map on {node} failed: {e}")
                raise

            # Received an HTTP response
            if response.status_code != 201:
                message = f"Putting treasure map on {node} failed with response status: {response.status}"
                self.log.warn(message)
            return response

        self._worker_pool = WorkerPool(worker=put_treasure_map_on_node,
                                       value_factory=AllAtOnceFactory(nodes),
                                       target_successes=self._block_until_this_many_are_complete,
                                       timeout=timeout,
                                       stagger_timeout=0,
                                       threadpool_size=threadpool_size)

    @property
    def completed(self):
        # TODO: lock dict before copying?
        return self._worker_pool.get_successes()

    def start(self):
        self.log.info(f"TreasureMapPublisher starting")
        self._worker_pool.start()
        if reactor.running:
            reactor.callInThread(self.block_until_complete)

    def block_until_success_is_reasonably_likely(self):
        # Note: `OutOfValues`/`TimedOut` may be raised here, which means we didn't even get to
        # `percent_to_complete_before_release` successes. For now just letting it fire.
        self._worker_pool.block_until_target_successes()
        completed = self.completed
        self.log.debug(f"The minimal amount of nodes ({len(completed)}) were contacted "
                       "while blocking for treasure map publication.")

        successes = self._worker_pool.get_successes()
        responses = {ursula.checksum_address: status for ursula, status in successes.items()}
        if not all(response.status_code == 201 for response in responses.values()):
            report = "\n".join(f"{address}: {status}" for address, status in responses.items())
            self.log.debug(f"Policy enactment failed. Request statuses:\n{report}")

            # OK, let's check: if any Ursulas claimed we didn't pay,
            # we need to re-evaluate our situation here.
            claims_of_freeloading = any(response.status_code == 402 for response in responses.values())
            if claims_of_freeloading:
                raise Policy.Unpaid

            # otherwise just raise a more generic error
            raise Policy.EnactmentError(report)

        return completed

    def block_until_complete(self):
        self._worker_pool.join()


class Policy(ABC):
    """
    An edict by Alice, arranged with n Ursulas, to perform re-encryption for a specific Bob.
    """

    ID_LENGTH = POLICY_ID_LENGTH

    log = Logger("Policy")

    class PolicyException(Exception):
        """Base exception for policy exceptions"""

    class NotEnoughUrsulas(PolicyException):
        """
        Raised when a Policy has been used to generate Arrangements with Ursulas insufficient number
        such that we don't have enough KeyFrags to give to each Ursula.
        """

    class EnactmentError(PolicyException):
        """Raised if one or more Ursulas failed to enact the policy."""

    class Unpaid(PolicyException):
        """Raised when a worker expects policy payment but receives none."""

    class Unknown(PolicyException):
        """Raised when a worker cannot find a published policy for a given policy ID"""

    class Inactive(PolicyException):
        """Raised when a worker is requested to perform re-encryption for a disabled policy"""

    class Expired(PolicyException):
        """Raised when a worker is requested to perform re-encryption for an expired policy"""

    class Unauthorized(PolicyException):
        """Raised when Bob is not authorized to request re-encryptions from Ursula.."""

    class Revoked(Unauthorized):
        """Raised when a policy is revoked has been revoked access"""

    def __init__(self,
                 publisher: 'Alice',
                 label: bytes,
                 expiration: maya.MayaDT,
                 bob: 'Bob',
                 kfrags: Sequence[VerifiedKeyFrag],
                 public_key: PublicKey,
                 m: int,
                 ):

        """
        :param kfrags:  A list of KeyFrags to distribute per this Policy.
        :param label: The identity of the resource to which Bob is granted access.
        """

        self.m = m
        self.n = len(kfrags)
        self.publisher = publisher
        self.label = label
        self.bob = bob
        self.kfrags = kfrags
        self.public_key = public_key
        self.expiration = expiration

        self._id = construct_policy_id(self.label, bytes(self.bob.stamp))

        """
        # TODO: #180 - This attribute is hanging on for dear life.
        After 180 is closed, it can be completely deprecated.

        The "hashed resource authentication code".

        A hash of:
        * Alice's public key
        * Bob's public key
        * the label

        Alice and Bob have all the information they need to construct this.
        'Ursula' does not, so we share it with her.
        """
        self.hrac = TreasureMap.derive_hrac(publisher_verifying_key=self.publisher.stamp.as_umbral_pubkey(),
                                            bob_verifying_key=self.bob.stamp.as_umbral_pubkey(),
                                            label=self.label)

    def __repr__(self):
        return f"{self.__class__.__name__}:{self._id.hex()[:6]}"

    @abstractmethod
    def _make_reservoir(self, handpicked_addresses: Sequence[ChecksumAddress]) -> MergedReservoir:
        """
        Builds a `MergedReservoir` to use for drawing addresses to send proposals to.
        """
        raise NotImplementedError

    def _enact_arrangements(self, arrangements: Dict['Ursula', Arrangement]):
        pass

    def _propose_arrangement(self,
                             address: ChecksumAddress,
                             network_middleware: RestMiddleware,
                             ) -> Tuple['Ursula', Arrangement]:
        """
        Attempt to propose an arrangement to the node with the given address.
        """

        if address not in self.publisher.known_nodes:
            raise RuntimeError(f"{address} is not known")

        ursula = self.publisher.known_nodes[address]
        arrangement = Arrangement.from_publisher(publisher=self.publisher, expiration=self.expiration)

        self.log.debug(f"Proposing arrangement {arrangement} to {ursula}")
        negotiation_response = network_middleware.propose_arrangement(ursula, arrangement)
        status = negotiation_response.status_code

        if status == 200:
            # TODO: What to do in the case of invalid signature?
            # Verify that the sampled ursula agreed to the arrangement.
            ursula_signature = negotiation_response.content
            self.publisher.verify_from(ursula,
                                       bytes(arrangement),
                                       signature=Signature.from_bytes(ursula_signature),
                                       decrypt=False)
            self.log.debug(f"Arrangement accepted by {ursula}")
        else:
            message = f"Proposing arrangement to {ursula} failed with {status}"
            self.log.debug(message)
            raise RuntimeError(message)

        # We could just return the arrangement and get the Ursula object
        # from `known_nodes` later, but when we introduce slashing in FleetSensor,
        # the address can already disappear from `known_nodes` by that time.
        return ursula, arrangement

    def _make_arrangements(self,
                           network_middleware: RestMiddleware,
                           handpicked_ursulas: Optional[Iterable['Ursula']] = None,
                           timeout: int = 10,
                           ) -> Dict['Ursula', Arrangement]:
        """
        Pick some Ursula addresses and send them arrangement proposals.
        Returns a dictionary of Ursulas to Arrangements if it managed to get `n` responses.
        """

        if handpicked_ursulas is None:
            handpicked_ursulas = []
        handpicked_addresses = [ChecksumAddress(ursula.checksum_address) for ursula in handpicked_ursulas]

        reservoir = self._make_reservoir(handpicked_addresses)
        value_factory = PrefetchStrategy(reservoir, self.n)

        def worker(address):
            return self._propose_arrangement(address, network_middleware)

        self.publisher.block_until_number_of_known_nodes_is(self.n, learn_on_this_thread=True, eager=True)

        worker_pool = WorkerPool(worker=worker,
                                 value_factory=value_factory,
                                 target_successes=self.n,
                                 timeout=timeout,
                                 stagger_timeout=1,
                                 threadpool_size=self.n)
        worker_pool.start()
        try:
            successes = worker_pool.block_until_target_successes()
        except (WorkerPool.OutOfValues, WorkerPool.TimedOut):
            # It's possible to raise some other exceptions here,
            # but we will use the logic below.
            successes = worker_pool.get_successes()
        finally:
            worker_pool.cancel()
            worker_pool.join()

        accepted_arrangements = {ursula: arrangement for ursula, arrangement in successes.values()}
        failures = worker_pool.get_failures()

        accepted_addresses = ", ".join(ursula.checksum_address for ursula in accepted_arrangements)

        if len(accepted_arrangements) < self.n:

            rejected_proposals = "\n".join(f"{address}: {value}" for address, (type_, value, traceback) in failures.items())

            self.log.debug(
                "Could not find enough Ursulas to accept proposals.\n"
                f"Accepted: {accepted_addresses}\n"
                f"Rejected:\n{rejected_proposals}")

            raise self._not_enough_ursulas_exception()
        else:
            self.log.debug(f"Finished proposing arrangements; accepted: {accepted_addresses}")

        return accepted_arrangements

    def _make_publisher(self,
                        treasure_map: 'EncryptedTreasureMap',
                        network_middleware: RestMiddleware,
                        ) -> TreasureMapPublisher:

        # TODO (#2516): remove hardcoding of 8 nodes
        self.publisher.block_until_number_of_known_nodes_is(8, timeout=2, learn_on_this_thread=True)
        target_nodes = self.bob.matching_nodes_among(self.publisher.known_nodes)
        treasure_map_bytes = bytes(treasure_map)  # prevent holding of the reference

        return TreasureMapPublisher(treasure_map_bytes=treasure_map_bytes,
                                    nodes=target_nodes,
                                    network_middleware=network_middleware)

    def _encrypt_treasure_map(self, treasure_map):
        return treasure_map.prepare_for_publication(self.publisher, self.bob)

    def enact(self,
              network_middleware: RestMiddleware,
              handpicked_ursulas: Optional[Iterable['Ursula']] = None,
              publish_treasure_map: bool = True,
              ) -> 'EnactedPolicy':
        """
        Attempts to enact the policy, returns an `EnactedPolicy` object on success.
        """

        # TODO: Why/is this needed here?
        # Workaround for `RuntimeError: Learning loop is not running.  Start it with start_learning().`
        if not self.publisher._learning_task.running:
            self.publisher.start_learning_loop()

        arrangements = self._make_arrangements(network_middleware=network_middleware,
                                               handpicked_ursulas=handpicked_ursulas)

        self._enact_arrangements(arrangements)

        treasure_map = TreasureMap.construct_by_publisher(publisher=self.publisher,
                                                          bob=self.bob,
                                                          label=self.label,
                                                          ursulas=list(arrangements),
                                                          verified_kfrags=self.kfrags,
                                                          m=self.m)

        enc_treasure_map = self._encrypt_treasure_map(treasure_map)

        treasure_map_publisher = self._make_publisher(treasure_map=enc_treasure_map,
                                                      network_middleware=network_middleware)

        # TODO: Signal revocation without using encrypted kfrag
        revocation_kit = RevocationKit(treasure_map=treasure_map, signer=self.publisher.stamp)

        enacted_policy = EnactedPolicy(self._id,
                                       self.hrac,
                                       self.label,
                                       self.public_key,
                                       treasure_map.m,
                                       enc_treasure_map,
                                       treasure_map_publisher,
                                       revocation_kit,
                                       self.publisher.stamp.as_umbral_pubkey())

        if publish_treasure_map is True:
            enacted_policy.publish_treasure_map()

        return enacted_policy

    @abstractmethod
    def _not_enough_ursulas_exception(self) -> Type[Exception]:
        """
        Returns an exception to raise when there were not enough Ursulas
        to distribute arrangements to.
        """
        raise NotImplementedError

    @abstractmethod
    def _make_enactment_payload(self, kfrag: VerifiedKeyFrag) -> bytes:
        """
        Serializes a given kfrag and policy publication transaction to send to Ursula.
        """
        raise NotImplementedError


class FederatedPolicy(Policy):

    def _not_enough_ursulas_exception(self):
        return Policy.NotEnoughUrsulas

    def _make_reservoir(self, handpicked_addresses):
        return make_federated_staker_reservoir(known_nodes=self.publisher.known_nodes,
                                               include_addresses=handpicked_addresses)

    def _make_enactment_payload(self, kfrag) -> bytes:
        return bytes(kfrag)


class BlockchainPolicy(Policy):
    """
    A collection of n Arrangements representing a single Policy
    """

    class InvalidPolicyValue(ValueError):
        pass

    class NotEnoughBlockchainUrsulas(Policy.NotEnoughUrsulas):
        pass

    def __init__(self,
                 value: int,
                 rate: int,
                 payment_periods: int,
                 *args,
                 **kwargs):

        super().__init__(*args, **kwargs)

        self.payment_periods = payment_periods
        self.value = value
        self.rate = rate

        self._validate_fee_value()

    def _not_enough_ursulas_exception(self):
        return BlockchainPolicy.NotEnoughBlockchainUrsulas

    def _validate_fee_value(self) -> None:
        rate_per_period = self.value // self.n // self.payment_periods  # wei
        recalculated_value = self.payment_periods * rate_per_period * self.n
        if recalculated_value != self.value:
            raise ValueError(f"Invalid policy value calculation - "
                             f"{self.value} can't be divided into {self.n} staker payments per period "
                             f"for {self.payment_periods} periods without a remainder")

    @staticmethod
    def generate_policy_parameters(n: int,
                                   payment_periods: int,
                                   value: int = None,
                                   rate: int = None) -> dict:

        # Check for negative inputs
        if sum(True for i in (n, payment_periods, value, rate) if i is not None and i < 0) > 0:
            raise BlockchainPolicy.InvalidPolicyValue(f"Negative policy parameters are not allowed. Be positive.")

        # Check for policy params
        if not (bool(value) ^ bool(rate)):
            if not (value == 0 or rate == 0):  # Support a min fee rate of 0
                raise BlockchainPolicy.InvalidPolicyValue(f"Either 'value' or 'rate'  must be provided for policy. "
                                                          f"Got value: {value} and rate: {rate}")

        if value is None:
            value = rate * payment_periods * n

        else:
            value_per_node = value // n
            if value_per_node * n != value:
                raise BlockchainPolicy.InvalidPolicyValue(f"Policy value of ({value} wei) cannot be"
                                                          f" divided by N ({n}) without a remainder.")

            rate = value_per_node // payment_periods
            if rate * payment_periods != value_per_node:
                raise BlockchainPolicy.InvalidPolicyValue(f"Policy value of ({value_per_node} wei) per node "
                                                          f"cannot be divided by duration ({payment_periods} periods)"
                                                          f" without a remainder.")

        params = dict(rate=rate, value=value)
        return params

    def _make_reservoir(self, handpicked_addresses):
        staker_reservoir = make_decentralized_staker_reservoir(staking_agent=self.publisher.staking_agent,
                                                               duration_periods=self.payment_periods,
                                                               include_addresses=handpicked_addresses)
        return staker_reservoir

    def _publish_to_blockchain(self, ursulas) -> dict:

        addresses = [ursula.checksum_address for ursula in ursulas]

        # Transact  # TODO: Move this logic to BlockchainPolicyActor
        receipt = self.publisher.policy_agent.create_policy(
            policy_id=self.hrac,  # bytes16 _policyID
            transacting_power=self.publisher.transacting_power,
            value=self.value,
            end_timestamp=self.expiration.epoch,  # uint16 _numberOfPeriods
            node_addresses=addresses  # address[] memory _nodes
        )

        # Capture Response
        return receipt['transactionHash']

    def _make_enactment_payload(self, kfrag) -> bytes:
        return bytes(self.hrac)[:self.ID_LENGTH] + bytes(kfrag)

    def _enact_arrangements(self, arrangements: Dict['Ursula', Arrangement]) -> None:
        self._publish_to_blockchain(ursulas=list(arrangements))

    def _encrypt_treasure_map(self, treasure_map):
        transacting_power = self.publisher._crypto_power.power_ups(TransactingPower)
        return treasure_map.prepare_for_publication(
            self.publisher,
            self.bob,
            blockchain_signer=transacting_power.sign_message)


class EnactedPolicy:

    def __init__(self,
                 id: bytes,
                 hrac: bytes,
                 label: bytes,
                 public_key: PublicKey,
                 m: int,
                 treasure_map: 'EncryptedTreasureMap',
                 treasure_map_publisher: TreasureMapPublisher,
                 revocation_kit: RevocationKit,
                 publisher_verifying_key: PublicKey,
                 ):

        self.id = id # TODO: is it even used anywhere?
        self.hrac = hrac
        self.label = label
        self.public_key = public_key
        self.treasure_map = treasure_map
        self.treasure_map_publisher = treasure_map_publisher
        self.revocation_kit = revocation_kit
        self.m = m
        self.n = len(self.revocation_kit)
        self.publisher_verifying_key = publisher_verifying_key

    def publish_treasure_map(self):
        self.treasure_map_publisher.start()
