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
from urllib.parse import urlparse

import random

import requests
from requests.exceptions import RequestException, HTTPError
from typing import Union

from nucypher.blockchain.economics import StandardTokenEconomics
from nucypher.characters.lawful import Ursula
from nucypher.config.storages import LocalFileBasedNodeStorage
from nucypher.network.middleware import RestMiddleware


class UnknownIPAddress(RuntimeError):
    pass


RequestErrors = (
    # https://requests.readthedocs.io/en/latest/user/quickstart/#errors-and-exceptions
    ConnectionError,
    TimeoutError,
    RequestException,
    HTTPError
)


def get_external_ip_from_url_source(url: str, certificate=None) -> Union[str, None]:
    """Certificate is needed if the remote URL source is self-signed."""
    try:
        # 'None' or 'True' will verify self-signed certificates
        response = requests.get(url, verify=certificate)
    except RequestErrors:
        return None
    if response.status_code == 200:
        return response.text


def get_external_ip_from_default_teacher(network: str, federated_only: bool = False) -> Union[str, None]:

    try:
        top_teacher_url = RestMiddleware.TEACHER_NODES[network][0]
    except (KeyError, IndexError):
        # unknown network or no default teachers available
        return  # just move on.
    teacher = Ursula.from_teacher_uri(teacher_uri=top_teacher_url,
                                      federated_only=federated_only,
                                      min_stake=StandardTokenEconomics._default_minimum_allowed_locked)
    response = teacher.network_middleware.ping()
    if response.status_code == 200:
        return response.text


def get_external_ip_from_known_nodes(known_nodes, sample_size: int = 3):
    sample = random.sample(known_nodes, sample_size)
    for node in sample:
        ip = get_external_ip_from_url_source(url=node.rest_url())
        if ip:
            return ip


def get_external_ip_from_centralized_source() -> str:
    endpoint = 'https://ifconfig.me/'
    ip = get_external_ip_from_url_source(url=endpoint)
    return ip


def determine_external_ip_address(network: str, known_nodes=None) -> str:
    """
    Attempts to automatically get the external IP from the default teacher.
    If the request fails, it falls back to a centralized service.  If the IP address cannot be determined
    for any reason UnknownIPAddress is raised.
    """
    rest_host = None
    if known_nodes:  # primary
        rest_host = get_external_ip_from_known_nodes(known_nodes=known_nodes)
    if not rest_host:  # fallback 1
        rest_host = get_external_ip_from_default_teacher(network=network)
    if not rest_host:  # fallback 2
        rest_host = get_external_ip_from_centralized_source()
    if not rest_host:  # complete cascading failure
        raise UnknownIPAddress('External IP address detection failed')
    return rest_host
