import json
import logging
from os.path import commonpath
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import click
import requests
from click import ClickException, UsageError
from requests import RequestException

from fuzzing_cli.fuzz.exceptions import FaaSError, RPCCallError
from fuzzing_cli.fuzz.ide import IDEArtifacts
from fuzzing_cli.fuzz.lessons import FuzzingLessons
from fuzzing_cli.fuzz.quickcheck_lib.utils import mk_contract_address
from fuzzing_cli.fuzz.types import (
    Contract,
    EVMBlock,
    EVMTransaction,
    SeedSequenceTransaction,
)

from .generic import RPCClientBase

LOGGER = logging.getLogger("fuzzing-cli")

headers = {"Content-Type": "application/json"}
NUM_BLOCKS_UPPER_LIMIT = 9999

SEED_STATE = Dict[str, Any]
CONTRACT_ADDRESS = str
CONTRACT_BYTECODE = str


class MissingTargetsError(FaaSError):
    pass


class TargetsNotFoundError(FaaSError):
    pass


class RPCClient(RPCClientBase):
    def __init__(self, rpc_url: str, number_of_cores: int = 1):
        self.rpc_url = rpc_url
        self.number_of_cores = number_of_cores

    def call(self, method: str, params: List[Union[str, bool, int, float]]):
        """Make an rpc call to the RPC endpoint

        :return: Result property of the RPC response
        """
        try:
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
            response = (
                requests.request("POST", self.rpc_url, headers=headers, json=payload)
            ).json()
            return response.get("result", None)
        except RequestException as e:
            raise RPCCallError(
                f"HTTP error calling RPC method {method} with parameters: {params}"
                f"\nAre you sure the RPC is running at {self.rpc_url}?"
            )

    def get_block(
        self, latest: bool = False, block_number: int = -1
    ) -> Optional[EVMBlock]:
        block_value = "latest" if latest else str(block_number)
        if not latest:
            block_value = hex(block_number)

        block = self.call("eth_getBlockByNumber", [block_value, True])
        return block

    def get_block_by_hash(self, hash: str) -> Optional[EVMBlock]:
        block = self.call("eth_getBlockByHash", [hash, True])
        return block

    def get_code(
        self, contract_address: CONTRACT_ADDRESS
    ) -> Optional[CONTRACT_BYTECODE]:
        deployed_bytecode = self.call("eth_getCode", [contract_address, "latest"])
        if deployed_bytecode == "0x":
            return None
        return deployed_bytecode

    def get_all_blocks(self) -> List[EVMBlock]:
        """Get all blocks from the node running at rpc_url

        Raises an exception if the number of blocks
        exceeds 10000 as it is likely a user error who passed the wrong
        RPC address.
        """
        num_of_blocks = self.get_latest_block_number() + 1
        if num_of_blocks == 0:
            return []

        if num_of_blocks > NUM_BLOCKS_UPPER_LIMIT:
            raise click.exceptions.ClickException(
                "Number of blocks existing on the ethereum node running at "
                + str(self.rpc_url)
                + " can not exceed 10000. Did you pass the correct RPC url?"
            )
        blocks = []
        for i in range(0, num_of_blocks):
            blocks.append(self.get_block(block_number=i))
        return blocks

    def get_latest_block_number(self) -> int:
        latest_block = self.get_block(latest=True)
        if not latest_block:
            return -1
        num_of_blocks = int(latest_block["number"], 16)
        return num_of_blocks

    def get_transactions(
        self,
        blocks: Optional[List[EVMBlock]] = None,
        block_numbers_to_skip: List[str] = [],
    ) -> List[EVMTransaction]:
        if not blocks:
            blocks = self.get_all_blocks()
        processed_transactions = []
        for block in blocks:
            if block["number"] in block_numbers_to_skip:
                continue
            for transaction in block["transactions"]:
                for key, value in dict(transaction).items():
                    if value is None:
                        transaction[key] = ""
                transaction.update(
                    {
                        "blockCoinbase": block["miner"],
                        "blockDifficulty": block["difficulty"],
                        "blockGasLimit": block["gasLimit"],
                        "blockTimestamp": block["timestamp"],
                    }
                )
                processed_transactions.append(transaction)
        return processed_transactions

    def get_inconsistent_addresses(
        self, seed_state: SEED_STATE
    ) -> Tuple[Dict[CONTRACT_ADDRESS, CONTRACT_BYTECODE], List[CONTRACT_ADDRESS]]:
        """
        This function validates the seed state and returns the list of contracts that are
        either not deployed in the rpc node or not provided by the user.

        Parameters:
        seed_state (Dict[str, any]): The seed state is the list of transactions which are deployed on the RPC node. It also includes the list of contracts that the user wants to fuzz (addresses under test).

        Returns -> Tuple[Dict[str, str], List[str]]:
        [missing_targets, unknown_targets]: missing targets is the list of contracts that are in the RPC node but the user did not provide. unknown targets is the list of contracts that the user provided but are not deployed in the rpc node
        """
        deployed_contracts_addresses = self.get_all_deployed_contracts_addresses(
            seed_state
        )

        # This is the list of contracts that the user provided
        # but are not deployed in the rpc node
        unknown_target_addresses = []

        # This is the list of contracts that the user provided
        target_addresses = self.addresses_under_test(seed_state)

        # If a user provided address is not deployed in the rpc node
        # it will be added to the unknown_targets list
        for target_address in target_addresses:
            if target_address not in deployed_contracts_addresses:
                unknown_target_addresses.append(target_address)

        # This is the list of contracts that are in the RPC node
        # but the user did not provide. We collect the deployed_bytecode
        missing_target_addresses: Dict[CONTRACT_ADDRESS, CONTRACT_BYTECODE] = {}
        for contract_address in deployed_contracts_addresses:
            if contract_address not in target_addresses:
                missing_target_addresses[contract_address] = self.get_code(
                    contract_address
                )

        # We return the missing targets and the unknown targets as a tuple
        return missing_target_addresses, unknown_target_addresses

    def get_seed_state(
        self,
        address: str,
        other_addresses: Optional[List[str]],
        corpus_target: Optional[str] = None,
    ) -> Dict[str, any]:
        try:
            processed_transactions: List[EVMTransaction] = []
            blocks_to_skip: Set[str] = set({})
            suggested_seed_seqs: List[List[SeedSequenceTransaction]] = []

            for lesson in FuzzingLessons.get_lessons():
                click.secho(
                    f"Lesson \"{lesson['description']}\" will be added to the campaign's seed state"
                )
                LOGGER.debug(
                    f"Adding lesson \"{lesson['description']}\" to the campaign's seed state"
                )
                blocks_to_skip.update(
                    {b["blockNumber"] for s in lesson["transactions"] for b in s}
                )
                suggested_seed_seqs.extend(lesson["transactions"])

            LOGGER.debug(
                f"Skipping blocks {list(blocks_to_skip)} because they are part of the lessons"
            )
            processed_transactions.extend(
                self.get_transactions(block_numbers_to_skip=list(blocks_to_skip))
            )

            if len(processed_transactions) == 0:
                raise click.exceptions.UsageError(
                    f"Unable to generate the seed state for address {address}. "
                    f"No transactions were found in an ethereum node running at {self.rpc_url}"
                )

            setup = {
                "address-under-test": address,
                "steps": processed_transactions,
                "other-addresses-under-test": other_addresses,
            }
            """Get a seed state for the target contract to be used by Harvey"""
            if corpus_target:
                setup["target"] = corpus_target
            if len(suggested_seed_seqs) > 0:
                setup["suggested-seed-seqs"] = suggested_seed_seqs
            return {
                "discovery-probability-threshold": 0.0,
                "assertion-checking-mode": 1,
                "num-cores": self.number_of_cores,
                "analysis-setup": setup,
            }

        except ClickException:
            raise
        except Exception as e:
            LOGGER.warning(f"Could not generate seed state for address: {address}")
            raise click.exceptions.UsageError(
                (
                    "Unable to generate the seed state for address "
                    + str(address)
                    + ". Are you sure you passed the correct contract address?"
                )
            ) from e

    @staticmethod
    def path_inclusion_checker(paths: List[str]):
        directory_paths: List[str] = []
        file_paths: List[str] = []
        for _path in paths:
            if Path(_path).is_dir():
                directory_paths.append(_path)
            else:
                file_paths.append(_path)

        def inner_checker(path: str):
            if path in file_paths:
                # we have found exact file match
                return True
            # try to find folder match
            for dir_path in directory_paths:
                if commonpath([dir_path, path]) == dir_path:
                    # file is in the directory
                    return True
            return False

        return inner_checker

    def get_contract_by_address(
        self, contract_address: CONTRACT_ADDRESS, artifacts: IDEArtifacts
    ) -> Optional[Contract]:
        """Get the artifacts of the contracts at the given addresses"""
        deployed_bytecode = self.get_code(contract_address)
        if deployed_bytecode is None:  # it's unknown contract
            LOGGER.warning(
                f'No deployed bytecode is found in an RPC node for contract: "{contract_address}"'
            )
            return None
        contract = artifacts.get_contract(deployed_bytecode)
        if not contract or contract.get("mainSourceFile", None) is None:
            LOGGER.warning(
                f'Contract "{contract_address}" could not be found in sources.'
                f" You can try to manually set the sources using the targets option. "
                f"More at: https://fuzzing-docs.diligence.tools/getting-started/configuring-the-cli#configuration"
            )
            return None
        return contract

    def smart_mode_setup(
        self,
        smart_mode: bool,
        seed_state: Dict[str, any],
        source_targets: Optional[List[str]],
        artifacts: IDEArtifacts,
    ) -> Tuple[List[CONTRACT_ADDRESS], List[str]]:
        """Get the addresses under test and the targets for the campaign if smart mode is on
        or just return values provided by the user if smart mode is off"""

        addresses_under_test = self.addresses_under_test(seed_state)

        if smart_mode and addresses_under_test and source_targets:
            click.secho(
                "Warning: Smart mode is on, but targets and addresses under test are provided. "
                "Consider turning off smart mode, so that the targets and addresses under test won't be "
                "automatically derived from the seed state."
            )

        if not smart_mode and (not addresses_under_test or not source_targets):
            raise ClickException(
                "No targets nor addresses under test are provided. "
                "Please turn on smart mode or provide targets and addresses under test."
            )

        if not addresses_under_test:
            # addresses under test was not provided by the user, so get all the contracts addresses from the rpc client
            addresses_under_test = self.get_all_deployed_contracts_addresses(seed_state)

        targets: Set[str] = set(source_targets or [])
        # If the user not provided a list of source files as targets, we put every contract source file as a target
        if not source_targets:
            for contract_address in addresses_under_test:
                contract = self.get_contract_by_address(contract_address, artifacts)
                if contract:
                    targets.add(contract["mainSourceFile"])

        return list(addresses_under_test), list(targets)

    @staticmethod
    def get_all_deployed_contracts_addresses(
        seed_state: SEED_STATE,
    ) -> Set[CONTRACT_ADDRESS]:
        # Goes through the steps of the rpc node's txs and gets the addresses for each contract
        steps: List[EVMTransaction] = seed_state["analysis-setup"]["steps"]

        # This is the list of all the contract addresses that are deployed(created)
        # in the rpc(ganache) node.
        contracts: Set[str] = set()
        for txn in steps:
            # If "to" is empty, it means it's a contract creation
            if txn["to"]:
                continue
            # These are contract creation transactions
            contracts.add(
                mk_contract_address(
                    txn["from"][2:], int(txn["nonce"], base=16), prefix=True
                )
            )
        return contracts

    @staticmethod
    def addresses_under_test(seed_state: SEED_STATE) -> Set[CONTRACT_ADDRESS]:
        addresses: Set[str] = set()
        if seed_state["analysis-setup"]["address-under-test"]:
            addresses.add(seed_state["analysis-setup"]["address-under-test"])
        if seed_state["analysis-setup"].get("other-addresses-under-test", []):
            addresses.update(
                seed_state["analysis-setup"]["other-addresses-under-test"] or []
            )

        return {addr.lower() for addr in addresses}

    def collect_mismatched_targets(
        self,
        missing_target_addresses: Dict[CONTRACT_ADDRESS, CONTRACT_BYTECODE],
        artifacts: IDEArtifacts,
        source_targets: List[str],
    ):
        # We gather the list of contracts that have been deployed to the RPC node but
        # the user did not provide the contract address as a target.
        # It's best effort because there may be some addresses deployed whose source could
        # not be found in the project.
        missing_targets_resolved: List[Tuple[str, Optional[str], Optional[str]]] = []

        for contract_address, deployed_bytecode in missing_target_addresses.items():
            contract = self.get_contract_by_address(contract_address, artifacts)
            missing_targets_resolved.append(
                (
                    contract_address,
                    contract.get("mainSourceFile", "null") if contract else "null",
                    contract.get("contractName", "null") if contract else "null",
                )
            )

        # This is the case where a user marked the source file as a target in the targets field
        # but did not provide the address in addresses_under_test.
        # This error may not be necessary but we'll leave it here for the cases where users are explicit
        # about the targets (source files and addresses) they want to fuzz.
        mismatched_targets: List[Tuple[str, CONTRACT_ADDRESS]] = []
        for t in missing_targets_resolved:
            source_file = t[1]
            if source_file == "null":
                continue
            if source_file in source_targets:
                mismatched_targets.append((source_file, t[0]))

        return mismatched_targets, missing_targets_resolved

    def collect_dangling_contracts(
        self,
        addresses_under_test: List[CONTRACT_ADDRESS],
        artifacts: IDEArtifacts,
        source_targets: List[str],
    ):
        # contracts set as address under test but we don't know the contract object.
        # Because we couldn't make a correlation between the address and the deployed bytecode.
        # This could happen when the metadata hashing is not enabled.
        dangling_contract_targets: List[Tuple[Optional[str], str]] = []
        check_path = self.path_inclusion_checker(source_targets)

        for contract_address in addresses_under_test:
            # correlate to the source file
            # get code invokes an rpc call to get the deployed bytecode of the contract with address t.
            contract = self.get_contract_by_address(contract_address, artifacts)

            if contract is None:
                LOGGER.debug(
                    f'No contract is found in an RPC node for address: "{contract_address}"'
                )
                dangling_contract_targets.append((None, contract_address))
                continue

            if (
                contract.get("mainSourceFile", None) is None
                # check_path could fail when the main source file hasn't been included in the targets.
                or not check_path(artifacts.normalize_path(contract["mainSourceFile"]))
            ):
                LOGGER.debug(
                    f"Adding contract to dangling contracts list. Contract: {json.dumps(contract)}"
                )
                dangling_contract_targets.append(
                    (contract.get("mainSourceFile", None), contract_address)
                )

        return dangling_contract_targets

    @staticmethod
    def handle_unknown_target_addresses(unknown_target_addresses):
        raise ClickException(
            f"Unable to find contracts deployed at {', '.join(unknown_target_addresses)}"
        )

    @staticmethod
    def handle_mismatched_targets(mismatched_targets):
        data = "\n".join(
            [f"  ◦ Target: {t} Address: {a}" for t, a in mismatched_targets]
        )
        raise ClickException(
            f"Found contracts deployed at the following addresses "
            f"but they are not marked as addresses under test: \n{data}"
        )

    @staticmethod
    def handle_dangling_contracts(dangling_contract_targets):
        data = "\n".join(
            [f"  ◦ Address: {a} Target: {t}" for t, a in dangling_contract_targets]
        )
        raise ClickException(
            f"Found contracts marked as addresses under test but their source files were not provided as targets: \n{data}"
        )

    @staticmethod
    def handle_missing_target_addresses(missing_targets_resolved):
        data = "\n".join(
            [
                f"  ◦ Address: {t[0]} Source File: {t[1]} Contract Name: {t[2]}"
                for t in missing_targets_resolved
            ]
        )
        click.secho(
            f"⚠️ Following contracts were not marked as targets but were deployed to RPC node:\n{data}"
        )

    def check_contracts(
        self,
        seed_state: Dict[str, any],
        artifacts: IDEArtifacts,
        source_targets: Optional[List[str]],
        smart_mode: bool,
    ):
        """
        This function checks the contracts provided in the seed state and their addresses to ensure they are deployed correctly. It verifies that:

        All contracts deployed at the addresses specified in the addresses_under_test are found on the RPC.
        The source files of contracts that are deployed but not provided as targets are acknowledged.
        The addresses of contracts provided as targets without setting up their addresses in addresses_under_test are reported.
        The contracts specified in addresses_under_test without a corresponding contract object are identified.

        Parameters
        self : object
        The instance of the class the method is called on.
        seed_state : Dict[str, any]
        A dictionary containing the seed state data, including the addresses_under_test and other analysis setup information.
        artifacts : IDEArtifacts
        An instance of the IDEArtifacts class containing the contract artifacts.
        source_targets : List[str]
        A list of source file paths provided as targets for the analysis.

        Raises

        ClickException
        If there are unknown or mismatched targets, or if dangling contract targets are found.
        UsageError
        If there is an error during an RPC call.
        """

        # If smart mode is on, we get the addresses under test and the targets from the seed state
        # Otherwise, we get them from the user provided arguments
        (
            processed_addresses_under_test,
            processed_source_targets,
        ) = self.smart_mode_setup(smart_mode, seed_state, source_targets, artifacts)

        # Normalize the paths in source_targets
        # Make all paths absolute and not relative
        processed_source_targets = [
            artifacts.normalize_path(s) for s in processed_source_targets
        ]
        try:
            # Validate the seed state and obtain missing and unknown targets
            (
                missing_target_addresses,
                unknown_target_addresses,
            ) = self.get_inconsistent_addresses(seed_state)

            # The user provided an address that is not deployed in the rpc node
            if unknown_target_addresses:
                self.handle_unknown_target_addresses(unknown_target_addresses)

            (
                mismatched_targets,
                missing_targets_resolved,
            ) = self.collect_mismatched_targets(
                missing_target_addresses, artifacts, processed_source_targets
            )

            if mismatched_targets:
                self.handle_mismatched_targets(mismatched_targets)

            # An acknowledgement message to the user that some contracts were not included in the seed state
            # This is probably intended by the user but we'll let them know in case it's not.
            if missing_targets_resolved:
                self.handle_missing_target_addresses(missing_targets_resolved)

            dangling_contract_targets = self.collect_dangling_contracts(
                processed_addresses_under_test, artifacts, processed_source_targets
            )

            if dangling_contract_targets:
                self.handle_dangling_contracts(dangling_contract_targets)

        except RPCCallError as e:
            raise UsageError(f"{e}")
        except:
            raise
