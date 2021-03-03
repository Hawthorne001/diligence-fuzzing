import logging
import requests
import click
from mythx_cli.analyze.solidity import SolidityJob
import json

LOGGER = logging.getLogger("mythx-cli")

rpc_url = "http://localhost:7545"

headers = {
    'Content-Type': 'application/json'
}

time_limit_seconds = 3000


def rpc_call(method: str, params: str):
    payload = "{\"jsonrpc\":\"2.0\",\"method\":\"" + method + "\",\"params\":" + params + ",\"id\":1}"
    response = (requests.request("POST", rpc_url, headers=headers, data=payload)).json()
    return response["result"]


def get_block(latest: bool = False, block_number: int = -1):
    block_value = "latest" if latest else str(block_number)
    if not latest:
        block_value = hex(block_number)

    block = rpc_call("eth_getBlockByNumber", "[\"" + block_value + "\", true]")
    if block is None:
        return None
    else:
        return block


def get_all_blocks():
    latest_block = get_block(True)
    if not latest_block:
        return []

    blocks = []
    for i in range(0, int(latest_block["number"], 16) + 1, 1):
        blocks.append(get_block(block_number=i))
    return blocks


def get_seed_state(address: str, other_addresses: [str]):
    blocks = get_all_blocks()
    processed_transactions = []
    for block in blocks:
        for transaction in block["transactions"]:
            for key, value in dict(transaction).items():
                if value is None:
                    transaction[key] = ""
            processed_transactions.append(transaction)
    setup = dict({
        "address-under-test": address,
        "steps": processed_transactions,
        "other-addresses-under-test": other_addresses})
    return dict(
        {
            "time-limit-secs": time_limit_seconds,
            "analysis-setup": setup,
            "discovery-probability-threshold": 0.0,
            "assertion-checking-mode": 1,
            "emit-mythx-report": True
        }
    )


def camel_case_payload(payload):
    payload["mainSource"] = payload.pop("main_source")
    payload["solcVersion"] = payload.pop("solc_version")
    payload["contractName"] = payload.pop("contract_name")
    payload["sourceMap"] = payload.pop("source_map")
    payload["sourceList"] = payload.pop("source_list")
    payload["deployedBytecode"] = payload.pop("deployed_bytecode")
    payload["deployedSourceMap"] = payload.pop("deployed_source_map")


@click.command("run")
@click.option(
    "-a",
    "--address",
    type=click.STRING,
    help="Address of the main contract to analyze",
)
@click.option(
    "-m",
    "--more-addresses",
    type=click.STRING,
    help="Addresses of other contracts to analyze, separated by commas",
)
@click.argument("target", default=None, nargs=-1, required=False)
@click.pass_obj
def fuzz_run(ctx, address, more_addresses, target):
    # read YAML config params from ctx dict, e.g. ganache rpc url
    #   Introduce a separate `fuzz` section in the YAML file

    # construct seed state from ganache

    # construct the FaaS campaign object
    #   helpful method: mythx_cli/analyze/solidity.py:SolidityJob.generate_payloads
    #   NOTE: This currently patches link placeholders in the creation
    #         bytecode with the zero address. If we need to submit bytecode from
    #         solc compilation, we need to find a way to replace these with the Ganache
    #         instance's addresses. Preferably we pull all of this data from Ganache
    #         itself and just enrich the payload with source and AST data from the
    #         SolidityJob payload list

    # submit the FaaS payload, do error handling

    # print FaaS dashbaord url pointing to campaign

    contract_address = address
    contract_code_response = rpc_call("eth_getCode", "[\"" + contract_address + "\",\"latest\"]")

    if contract_code_response is None:
        print("Invalid address")

    if more_addresses is None:
        other_addresses=[]
    else:
        other_addresses = more_addresses.split(',')

    seed_state = get_seed_state(contract_address, other_addresses)
    payloads = []
    for t in target:
        sol = SolidityJob(target=t)
        sol.generate_payloads(version="0.6.12")
        camel_case_payload(sol.payloads[0])
        payloads.append(sol.payloads[0])

    data = {
        "execution": seed_state,
        "input": payloads
    }

    print(json.dumps(data))
    pass
