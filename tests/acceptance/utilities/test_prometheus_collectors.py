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

import random
from typing import List

import pytest

from nucypher.blockchain.eth.agents import ContractAgency, PREApplicationAgent
from tests.constants import TEST_ETH_PROVIDER_URI

try:
    # all prometheus related imports
    from prometheus_client import CollectorRegistry

    # include dependencies that have sub-dependencies on prometheus
    from nucypher.utilities.prometheus.collector import (
        BlockchainMetricsCollector,
        MetricsCollector,
        OperatorMetricsCollector,
        StakingProviderMetricsCollector,
        UrsulaInfoMetricsCollector,
    )
    from nucypher.utilities.prometheus.metrics import create_metrics_collectors

    # flag to skip tests
    PROMETHEUS_INSTALLED = True
except ImportError:
    PROMETHEUS_INSTALLED = False


@pytest.mark.skipif(condition=(not PROMETHEUS_INSTALLED), reason="prometheus_client is required for test")
def test_ursula_info_metrics_collector(test_registry,
                                       blockchain_ursulas,
                                       agency):
    ursula = random.choice(blockchain_ursulas)
    collector = UrsulaInfoMetricsCollector(ursula=ursula)

    collector_registry = CollectorRegistry()
    prefix = 'test_ursula_info_metrics_collector'
    collector.initialize(metrics_prefix=prefix, registry=collector_registry)
    collector.collect()

    mode = "running" if ursula._learning_task.running else "stopped"
    learning_mode = collector_registry.get_sample_value(
        f"{prefix}_node_discovery", labels={f"{prefix}_node_discovery": f"{mode}"}
    )
    assert learning_mode == 1

    known_nodes = collector_registry.get_sample_value(f"{prefix}_known_nodes")
    assert known_nodes == len(ursula.known_nodes)

    reencryption_requests = collector_registry.get_sample_value(
        f"{prefix}_reencryption_requests"
    )
    assert reencryption_requests == 0


@pytest.mark.skipif(condition=(not PROMETHEUS_INSTALLED), reason="prometheus_client is required for test")
def test_blockchain_metrics_collector(testerchain):
    collector = BlockchainMetricsCollector(eth_provider_uri=TEST_ETH_PROVIDER_URI)

    collector_registry = CollectorRegistry()
    prefix = 'test_blockchain_metrics_collector'
    collector.initialize(metrics_prefix=prefix, registry=collector_registry)
    collector.collect()

    metric_name = f"{prefix}_eth_chain_id"
    assert metric_name in collector_registry._names_to_collectors.keys()
    chain_id = collector_registry.get_sample_value(f"{prefix}_eth_chain_id")
    assert chain_id == testerchain.client.chain_id

    metric_name = f"{prefix}_eth_block_number"
    assert metric_name in collector_registry._names_to_collectors.keys()
    block_number = collector_registry.get_sample_value(metric_name)
    assert block_number == testerchain.get_block_number()


@pytest.mark.skipif(condition=(not PROMETHEUS_INSTALLED), reason="prometheus_client is required for test")
def test_staking_provider_metrics_collector(test_registry, staking_providers):
    staking_provider_address = random.choice(staking_providers)
    collector = StakingProviderMetricsCollector(
        staking_provider_address=staking_provider_address,
        contract_registry=test_registry,
    )
    collector_registry = CollectorRegistry()
    prefix = "test_staking_provider_metrics_collector"
    collector.initialize(metrics_prefix=prefix, registry=collector_registry)
    collector.collect()

    pre_application_agent = ContractAgency.get_agent(
        PREApplicationAgent, registry=test_registry
    )

    active_stake = collector_registry.get_sample_value(
        f"{prefix}_associated_active_stake"
    )
    # only floats can be stored
    assert active_stake == float(
        int(
            pre_application_agent.get_authorized_stake(
                staking_provider=staking_provider_address
            )
        )
    )

    staking_provider_info = pre_application_agent.get_staking_provider_info(
        staking_provider=staking_provider_address
    )

    operator_confirmed = collector_registry.get_sample_value(
        f"{prefix}_operator_confirmed"
    )
    assert operator_confirmed == staking_provider_info.operator_confirmed

    operator_start = collector_registry.get_sample_value(
        f"{prefix}_operator_start_timestamp"
    )
    assert operator_start == staking_provider_info.operator_start_timestamp


@pytest.mark.skipif(condition=(not PROMETHEUS_INSTALLED), reason="prometheus_client is required for test")
def test_operator_metrics_collector(test_registry, blockchain_ursulas):
    ursula = random.choice(blockchain_ursulas)
    collector = OperatorMetricsCollector(
        domain=ursula.domain,
        operator_address=ursula.operator_address,
        contract_registry=test_registry,
    )
    collector_registry = CollectorRegistry()
    prefix = 'test_worker_metrics_collector'
    collector.initialize(metrics_prefix=prefix, registry=collector_registry)
    collector.collect()

    operator_eth = collector_registry.get_sample_value(f"{prefix}_operator_eth_balance")
    # only floats can be stored
    assert operator_eth == float(ursula.eth_balance)


@pytest.mark.skipif(condition=(not PROMETHEUS_INSTALLED), reason="prometheus_client is required for test")
def test_all_metrics_collectors_sanity_collect(blockchain_ursulas):
    ursula = random.choice(blockchain_ursulas)

    collector_registry = CollectorRegistry()
    prefix = 'test_all_metrics_collectors'

    metrics_collectors = create_metrics_collectors(ursula=ursula)
    initialize_collectors(metrics_collectors=metrics_collectors,
                          collector_registry=collector_registry,
                          prefix=prefix)

    for collector in metrics_collectors:
        collector.collect()


def initialize_collectors(metrics_collectors: List['MetricsCollector'],
                          collector_registry: 'CollectorRegistry',
                          prefix: str) -> None:
    for collector in metrics_collectors:
        collector.initialize(metrics_prefix=prefix, registry=collector_registry)
