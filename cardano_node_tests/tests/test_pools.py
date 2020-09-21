import logging
from pathlib import Path
from typing import List

import allure
import hypothesis
import hypothesis.strategies as st
import pytest
from _pytest.tmpdir import TempdirFactory

from cardano_node_tests.utils import clusterlib
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import parallel_run
from cardano_node_tests.utils.types import OptionalFiles

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def temp_dir(tmp_path_factory: TempdirFactory):
    """Create a temporary dir and change to it."""
    tmp_path = Path(tmp_path_factory.mktemp("test_pools"))
    with helpers.change_cwd(tmp_path):
        yield tmp_path


@pytest.fixture
def cluster_mincost(cluster_manager: parallel_run.ClusterManager) -> clusterlib.ClusterLib:
    """Update "minPoolCost" to 5000."""
    return cluster_manager.get(mark="minPoolCost", cleanup=True)


@pytest.fixture
def update_pool_cost(cluster_mincost: clusterlib.ClusterLib):
    """Update "minPoolCost" to 5000."""
    helpers.update_params(
        cluster_obj=cluster_mincost,
        cli_arg="--min-pool-cost",
        param_name="minPoolCost",
        param_value=5000,
    )


# use the "temp_dir" fixture for all tests automatically
pytestmark = pytest.mark.usefixtures("temp_dir")


def _get_pool_ledger_state(
    cluster_obj: clusterlib.ClusterLib,
    stake_pool_id: str,
) -> dict:
    """Get ledger state of the pool."""
    stake_pool_id_dec = helpers.decode_bech32(stake_pool_id)
    pool_ledger_state: dict = (
        cluster_obj.get_registered_stake_pools_ledger_state().get(stake_pool_id_dec) or {}
    )
    return pool_ledger_state


def _check_pool(
    cluster_obj: clusterlib.ClusterLib,
    stake_pool_id: str,
    pool_data: clusterlib.PoolData,
):
    """Check and return ledger state of the pool."""
    pool_ledger_state: dict = _get_pool_ledger_state(
        cluster_obj=cluster_obj, stake_pool_id=stake_pool_id
    )

    assert pool_ledger_state, (
        "The newly created stake pool id is not shown inside the available stake pools;\n"
        f"Pool ID: {stake_pool_id} vs Existing IDs: "
        f"{list(cluster_obj.get_registered_stake_pools_ledger_state())}"
    )
    assert not helpers.check_pool_data(pool_ledger_state, pool_data)


def _check_staking(
    pool_owners: List[clusterlib.PoolUser],
    cluster_obj: clusterlib.ClusterLib,
    stake_pool_id: str,
):
    """Check that staking was correctly setup."""
    pool_ledger_state: dict = _get_pool_ledger_state(
        cluster_obj=cluster_obj, stake_pool_id=stake_pool_id
    )

    LOGGER.info("Waiting up to 3 epochs for stake pool to be registered.")
    helpers.wait_for(
        lambda: stake_pool_id in cluster_obj.get_stake_distribution(),
        delay=10,
        num_sec=3 * cluster_obj.epoch_length_sec,
        message="register stake pool",
    )

    for owner in pool_owners:
        stake_addr_info = cluster_obj.get_stake_addr_info(owner.stake.address)

        # check that the stake address was delegated
        assert stake_addr_info.delegation, f"Stake address was not delegated yet: {stake_addr_info}"
        assert stake_pool_id == stake_addr_info.delegation, "Stake address delegated to wrong pool"

        assert (
            # strip 'e0' from the beginning of the address hash
            helpers.decode_bech32(stake_addr_info.address)[2:]
            in pool_ledger_state["owners"]
        ), "'owner' value is different than expected"


def _create_register_pool(
    cluster_obj: clusterlib.ClusterLib,
    pool_owners: List[clusterlib.PoolUser],
    pool_data: clusterlib.PoolData,
) -> clusterlib.PoolCreationOutput:
    """Create and register a stake pool.

    Common functionality for tests.
    """
    src_address = pool_owners[0].payment.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # create and register pool
    pool_creation_out = cluster_obj.create_stake_pool(pool_data=pool_data, pool_owners=pool_owners)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance - pool_creation_out.tx_raw_output.fee - cluster_obj.get_pool_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    # check that pool was correctly setup
    _check_pool(
        cluster_obj=cluster_obj,
        stake_pool_id=pool_creation_out.stake_pool_id,
        pool_data=pool_data,
    )

    return pool_creation_out


def _create_register_pool_delegate_stake_tx(
    cluster_obj: clusterlib.ClusterLib,
    pool_owners: List[clusterlib.PoolUser],
    temp_template: str,
    pool_data: clusterlib.PoolData,
):
    """Create and register a stake pool, delegate stake address - all in single TX.

    Common functionality for tests.
    """
    # create node VRF key pair
    node_vrf = cluster_obj.gen_vrf_key_pair(node_name=pool_data.pool_name)
    # create node cold key pair and counter
    node_cold = cluster_obj.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

    # create stake address registration certs
    stake_addr_reg_cert_files = [
        cluster_obj.gen_stake_addr_registration_cert(
            addr_name=f"addr{i}_{temp_template}", stake_vkey_file=p.stake.vkey_file
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake address delegation cert
    stake_addr_deleg_cert_files = [
        cluster_obj.gen_stake_addr_delegation_cert(
            addr_name=f"addr{i}_{temp_template}",
            stake_vkey_file=p.stake.vkey_file,
            cold_vkey_file=node_cold.vkey_file,
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake pool registration cert
    pool_reg_cert_file = cluster_obj.gen_pool_registration_cert(
        pool_data=pool_data,
        vrf_vkey_file=node_vrf.vkey_file,
        cold_vkey_file=node_cold.vkey_file,
        owner_stake_vkey_files=[p.stake.vkey_file for p in pool_owners],
    )

    src_address = pool_owners[0].payment.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # register and delegate stake address, create and register pool
    tx_files = clusterlib.TxFiles(
        certificate_files=[
            pool_reg_cert_file,
            *stake_addr_reg_cert_files,
            *stake_addr_deleg_cert_files,
        ],
        signing_key_files=[
            *[p.payment.skey_file for p in pool_owners],
            *[p.stake.skey_file for p in pool_owners],
            node_cold.skey_file,
        ],
    )
    tx_raw_output = cluster_obj.send_tx(src_address=src_address, tx_files=tx_files)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance
        - tx_raw_output.fee
        - len(pool_owners) * cluster_obj.get_key_deposit()
        - cluster_obj.get_pool_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    # check that pool and staking were correctly setup
    stake_pool_id = cluster_obj.get_stake_pool_id(node_cold.vkey_file)
    _check_pool(cluster_obj=cluster_obj, stake_pool_id=stake_pool_id, pool_data=pool_data)
    _check_staking(
        pool_owners,
        cluster_obj=cluster_obj,
        stake_pool_id=stake_pool_id,
    )

    return clusterlib.PoolCreationOutput(
        stake_pool_id=stake_pool_id,
        vrf_key_pair=node_vrf,
        cold_key_pair=node_cold,
        pool_reg_cert_file=pool_reg_cert_file,
        pool_data=pool_data,
        pool_owners=pool_owners,
        tx_raw_output=tx_raw_output,
    )


def _create_register_pool_tx_delegate_stake_tx(
    cluster_obj: clusterlib.ClusterLib,
    pool_owners: List[clusterlib.PoolUser],
    temp_template: str,
    pool_data: clusterlib.PoolData,
) -> clusterlib.PoolCreationOutput:
    """Create and register a stake pool - first TX; delegate stake address - second TX.

    Common functionality for tests.
    """
    # create and register pool
    pool_creation_out = _create_register_pool(
        cluster_obj=cluster_obj, pool_owners=pool_owners, pool_data=pool_data
    )

    # create stake address registration certs
    stake_addr_reg_cert_files = [
        cluster_obj.gen_stake_addr_registration_cert(
            addr_name=f"addr{i}_{temp_template}", stake_vkey_file=p.stake.vkey_file
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake address delegation cert
    stake_addr_deleg_cert_files = [
        cluster_obj.gen_stake_addr_delegation_cert(
            addr_name=f"addr{i}_{temp_template}",
            stake_vkey_file=p.stake.vkey_file,
            cold_vkey_file=pool_creation_out.cold_key_pair.vkey_file,
        )
        for i, p in enumerate(pool_owners)
    ]

    src_address = pool_owners[0].payment.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # register and delegate stake address
    tx_files = clusterlib.TxFiles(
        certificate_files=[*stake_addr_reg_cert_files, *stake_addr_deleg_cert_files],
        signing_key_files=[
            *[p.payment.skey_file for p in pool_owners],
            *[p.stake.skey_file for p in pool_owners],
            pool_creation_out.cold_key_pair.skey_file,
        ],
    )
    tx_raw_output = cluster_obj.send_tx(src_address=src_address, tx_files=tx_files)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance - tx_raw_output.fee - len(pool_owners) * cluster_obj.get_key_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    # check that staking was correctly setup
    _check_staking(
        pool_owners,
        cluster_obj=cluster_obj,
        stake_pool_id=pool_creation_out.stake_pool_id,
    )

    return pool_creation_out


class TestStakePool:
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Create and register a stake pool with metadata."""
        temp_template = "test_stake_pool_metadata"

        pool_name = "cardano-node-tests"
        pool_metadata = {
            "name": pool_name,
            "description": "cardano-node-tests E2E tests",
            "ticker": "IOG1",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{pool_name}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=pool_name,
            pool_pledge=1000,
            pool_cost=15,
            pool_margin=0.2,
            pool_metadata_url="https://bit.ly/3bDUg9z",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=3,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool and delegate stake address
        _create_register_pool_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_not_avail(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Create and register a stake pool with metadata file not available."""
        temp_template = "test_stake_pool_metadata_not_avail"

        pool_name = f"pool_{clusterlib.get_rand_str(8)}"
        pool_metadata = {
            "name": pool_name,
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{pool_name}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=pool_name,
            pool_pledge=1000,
            pool_cost=15,
            pool_margin=0.2,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=1,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool and delegate stake address
        _create_register_pool_tx_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 3])
    @allure.link(helpers.get_vcs_link())
    def test_create_stake_pool(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        no_of_addr: int,
    ):
        """Create and register a stake pool."""
        temp_template = f"test_stake_pool_{no_of_addr}owners"

        pool_data = clusterlib.PoolData(
            pool_name=f"poolX_{no_of_addr}",
            pool_pledge=12345,
            pool_cost=123456789,
            pool_margin=0.123,
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool
        _create_register_pool(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            pool_data=pool_data,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 3])
    @allure.link(helpers.get_vcs_link())
    def test_deregister_stake_pool(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        no_of_addr: int,
    ):
        """Deregister stake pool."""
        temp_template = f"test_deregister_stake_pool_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolZ_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolZ_{no_of_addr}",
            pool_pledge=222,
            pool_cost=123,
            pool_margin=0.512,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool and delegate stake address
        pool_creation_out = _create_register_pool_tx_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

        pool_owner = pool_owners[0]
        src_register_balance = cluster.get_address_balance(pool_owner.payment.address)

        src_register_reward = cluster.get_stake_addr_info(
            pool_owner.stake.address
        ).reward_account_balance

        # deregister stake pool
        __, tx_raw_output = cluster.deregister_stake_pool(
            pool_owners=pool_owners,
            cold_key_pair=pool_creation_out.cold_key_pair,
            epoch=cluster.get_last_block_epoch() + 1,
            pool_name=pool_data.pool_name,
        )

        LOGGER.info("Waiting up to 3 epochs for stake pool to be deregistered.")
        stake_pool_id_dec = helpers.decode_bech32(pool_creation_out.stake_pool_id)
        helpers.wait_for(
            lambda: cluster.get_registered_stake_pools_ledger_state().get(stake_pool_id_dec)
            is None,
            delay=10,
            num_sec=3 * cluster.epoch_length_sec,
            message="deregister stake pool",
        )

        # check that the balance for source address was correctly updated
        assert src_register_balance - tx_raw_output.fee == cluster.get_address_balance(
            pool_owner.payment.address
        )

        # check that the stake addresses are no longer delegated
        for owner_rec in pool_owners:
            stake_addr_info = cluster.get_stake_addr_info(owner_rec.stake.address)
            assert (
                not stake_addr_info.delegation
            ), f"Stake address is still delegated: {stake_addr_info}"

        # check that the deposit was returned to reward account
        assert (
            cluster.get_stake_addr_info(pool_owner.stake.address).reward_account_balance
            == src_register_reward + cluster.get_pool_deposit()
        )

    @allure.link(helpers.get_vcs_link())
    def test_reregister_stake_pool(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Re-register stake pool."""
        temp_template = "test_reregister_stake_pool"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / "poolR_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name="poolR",
            pool_pledge=222,
            pool_cost=123,
            pool_margin=0.512,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(cluster_obj=cluster, name_template=temp_template)

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=1_500_000_000,
        )

        # register pool and delegate stake address
        pool_creation_out = _create_register_pool_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

        # deregister stake pool
        cluster.deregister_stake_pool(
            pool_owners=pool_owners,
            cold_key_pair=pool_creation_out.cold_key_pair,
            epoch=cluster.get_last_block_epoch() + 1,
            pool_name=pool_data.pool_name,
        )

        LOGGER.info("Waiting up to 3 epochs for stake pool to be deregistered.")
        stake_pool_id_dec = helpers.decode_bech32(pool_creation_out.stake_pool_id)
        helpers.wait_for(
            lambda: cluster.get_registered_stake_pools_ledger_state().get(stake_pool_id_dec)
            is None,
            delay=10,
            num_sec=3 * cluster.epoch_length_sec,
            message="deregister stake pool",
        )

        # check that the stake addresses are no longer delegated
        for owner_rec in pool_owners:
            stake_addr_info = cluster.get_stake_addr_info(owner_rec.stake.address)
            assert (
                not stake_addr_info.delegation
            ), f"Stake address is still delegated: {stake_addr_info}"

        src_address = pool_owners[0].payment.address
        src_init_balance = cluster.get_address_balance(src_address)

        # re-register the pool by resubmitting the pool registration certificate,
        # delegate stake address to pool again (the address is already registered)
        tx_files = clusterlib.TxFiles(
            certificate_files=[
                pool_creation_out.pool_reg_cert_file,
                *list(temp_dir.glob(f"*{temp_template}_stake_deleg.cert")),
            ],
            signing_key_files=pool_creation_out.tx_raw_output.tx_files.signing_key_files,
        )
        tx_raw_output = cluster.send_tx(src_address=src_address, tx_files=tx_files)
        cluster.wait_for_new_block(new_blocks=2)

        # check that the balance for source address was correctly updated
        assert (
            cluster.get_address_balance(src_address)
            == src_init_balance - tx_raw_output.fee - cluster.get_pool_deposit()
        ), f"Incorrect balance for source address `{src_address}`"

        LOGGER.info("Waiting up to 5 epochs for stake pool to be re-registered.")
        helpers.wait_for(
            lambda: pool_creation_out.stake_pool_id in cluster.get_stake_distribution(),
            delay=10,
            num_sec=5 * cluster.epoch_length_sec,
            message="re-register stake pool",
        )

        # check that pool was correctly setup
        _check_pool(
            cluster_obj=cluster, stake_pool_id=pool_creation_out.stake_pool_id, pool_data=pool_data
        )

        # check that the stake addresses were delegated
        _check_staking(
            pool_owners=pool_owners,
            cluster_obj=cluster,
            stake_pool_id=pool_creation_out.stake_pool_id,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 2])
    @allure.link(helpers.get_vcs_link())
    def test_update_stake_pool_metadata(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        no_of_addr: int,
    ):
        """Update stake pool metadata."""
        temp_template = f"test_update_stake_pool_metadata_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolA_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_metadata_updated = {
            "name": "QA_test_pool",
            "description": "pool description update",
            "ticker": "QA22",
            "homepage": "www.qa22.com",
        }
        pool_metadata_updated_file = helpers.write_json(
            temp_dir / f"poolA_{no_of_addr}_registration_metadata_updated.json",
            pool_metadata_updated,
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolA_{no_of_addr}",
            pool_pledge=4567,
            pool_cost=3,
            pool_margin=0.01,
            pool_metadata_url="https://init_location.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        pool_data_updated = pool_data._replace(
            pool_metadata_url="https://www.updated_location.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_updated_file),
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool
        pool_creation_out = _create_register_pool(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            pool_data=pool_data,
        )

        # update the pool parameters by resubmitting the pool registration certificate
        cluster.register_stake_pool(
            pool_data=pool_data_updated,
            pool_owners=pool_owners,
            vrf_vkey_file=pool_creation_out.vrf_key_pair.vkey_file,
            cold_key_pair=pool_creation_out.cold_key_pair,
            deposit=0,  # no additional deposit, the pool is already registered
        )
        cluster.wait_for_new_epoch()

        # check that the pool parameters were correctly updated on chain
        _check_pool(
            cluster_obj=cluster,
            stake_pool_id=pool_creation_out.stake_pool_id,
            pool_data=pool_data_updated,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 2])
    @allure.link(helpers.get_vcs_link())
    def test_update_stake_pool_parameters(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        no_of_addr: int,
    ):
        """Update stake pool parameters."""
        temp_template = f"test_update_stake_pool_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolB_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolB_{no_of_addr}",
            pool_pledge=4567,
            pool_cost=3,
            pool_margin=0.01,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        pool_data_updated = pool_data._replace(pool_pledge=1, pool_cost=1_000_000, pool_margin=0.9)

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool
        pool_creation_out = _create_register_pool(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            pool_data=pool_data,
        )

        # update the pool parameters by resubmitting the pool registration certificate
        cluster.register_stake_pool(
            pool_data=pool_data_updated,
            pool_owners=pool_owners,
            vrf_vkey_file=pool_creation_out.vrf_key_pair.vkey_file,
            cold_key_pair=pool_creation_out.cold_key_pair,
            deposit=0,  # no additional deposit, the pool is already registered
        )
        cluster.wait_for_new_epoch()

        # check that the pool parameters were correctly updated on chain
        _check_pool(
            cluster_obj=cluster,
            stake_pool_id=pool_creation_out.stake_pool_id,
            pool_data=pool_data_updated,
        )

    @allure.link(helpers.get_vcs_link())
    def test_sign_in_multiple_stages(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
    ):
        """Create and register a stake pool with TX signed in multiple stages."""
        temp_template = "test_sign_in_multiple_stages"

        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{clusterlib.get_rand_str()}",
            pool_pledge=5,
            pool_cost=3,
            pool_margin=0.01,
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=2,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # create node VRF key pair
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        # create node cold key pair and counter
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        # create stake pool registration cert
        pool_reg_cert_file = cluster.gen_pool_registration_cert(
            pool_data=pool_data,
            vrf_vkey_file=node_vrf.vkey_file,
            cold_vkey_file=node_cold.vkey_file,
            owner_stake_vkey_files=[p.stake.vkey_file for p in pool_owners],
        )

        src_address = pool_owners[0].payment.address
        src_init_balance = cluster.get_address_balance(src_address)

        # keys to sign the TX with
        witness_skeys = (
            pool_owners[0].payment.skey_file,
            pool_owners[1].payment.skey_file,
            pool_owners[0].stake.skey_file,
            pool_owners[1].stake.skey_file,
            node_cold.skey_file,
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[
                pool_reg_cert_file,
            ],
        )

        fee = cluster.calculate_tx_fee(
            src_address=src_address,
            tx_name=temp_template,
            tx_files=tx_files,
            witness_count_add=len(witness_skeys),
        )

        tx_raw_output = cluster.build_raw_tx(
            src_address=src_address,
            tx_files=tx_files,
            fee=fee,
        )

        # create witness file for each key
        witness_files: OptionalFiles = [
            cluster.witness_tx(
                tx_body_file=tx_raw_output.out_file, witness_signing_key_files=[skey]
            )
            for skey in witness_skeys
        ]

        # sign TX using witness files
        tx_witnessed_file = cluster.sign_witness_tx(
            tx_body_file=tx_raw_output.out_file, witness_files=witness_files
        )

        # create and register pool
        cluster.submit_tx(tx_witnessed_file)
        cluster.wait_for_new_block(new_blocks=2)

        # check that the balance for source address was correctly updated
        assert (
            cluster.get_address_balance(src_address)
            == src_init_balance - tx_raw_output.fee - cluster.get_pool_deposit()
        ), f"Incorrect balance for source address `{src_address}`"

        cluster.wait_for_new_epoch()

        # check that the pool parameters were correctly registered on chain
        stake_pool_id = cluster.get_stake_pool_id(node_cold.vkey_file)
        _check_pool(
            cluster_obj=cluster,
            stake_pool_id=stake_pool_id,
            pool_data=pool_data,
        )


@pytest.mark.usefixtures("temp_dir", "update_pool_cost")
@pytest.mark.run(order=1)
class TestPoolCost:
    @pytest.fixture
    def pool_owners(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster_mincost: clusterlib.ClusterLib,
    ):
        """Create class scoped pool owners."""
        data_key = id(self.pool_owners)
        cached_value = cluster_manager.cache.test_data.get(data_key)
        if cached_value:
            return cached_value  # type: ignore

        cluster = cluster_mincost
        rand_str = clusterlib.get_rand_str()
        temp_template = f"test_pool_cost_class_{rand_str}"

        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=1,
        )
        cluster_manager.cache.test_data[data_key] = pool_owners

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        return pool_owners

    @hypothesis.given(pool_cost=st.integers(max_value=4999))  # minPoolCost is now 5000
    @hypothesis.settings(deadline=None, suppress_health_check=(hypothesis.HealthCheck.too_slow,))
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_low_cost(
        self,
        cluster_mincost: clusterlib.ClusterLib,
        pool_owners: List[clusterlib.PoolUser],
        pool_cost: int,
    ):
        """Try to create and register a stake pool with pool cost lower than 'minPoolCost'."""
        cluster = cluster_mincost
        rand_str = clusterlib.get_rand_str()

        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{rand_str}",
            pool_pledge=12345,
            pool_cost=pool_cost,
            pool_margin=0.123,
        )

        # register pool, expect failure
        with pytest.raises(clusterlib.CLIError) as excinfo:
            _create_register_pool(
                cluster_obj=cluster,
                pool_owners=pool_owners,
                pool_data=pool_data,
            )

        # check that it failed in an expected way
        expected_msg = "--pool-cost: Failed reading" if pool_cost < 0 else "StakePoolCostTooLowPOOL"
        assert expected_msg in str(excinfo.value)

    @pytest.mark.parametrize("pool_cost", [5000, 9999999])
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_cost(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster_mincost: clusterlib.ClusterLib,
        pool_owners: List[clusterlib.PoolUser],
        pool_cost: int,
    ):
        """Create and register a stake pool with pool cost >= 'minPoolCost'."""
        cluster = cluster_mincost
        rand_str = clusterlib.get_rand_str()
        temp_template = f"test_stake_pool_cost_{rand_str}"

        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{rand_str}",
            pool_pledge=12345,
            pool_cost=pool_cost,
            pool_margin=0.123,
        )

        # create pool owners
        pool_owners = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template=temp_template,
            no_of_addr=1,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=900_000_000,
        )

        # register pool
        _create_register_pool(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            pool_data=pool_data,
        )


class TestNegative:
    @pytest.fixture
    def pool_users(
        self,
        cluster_manager: parallel_run.ClusterManager,
        cluster: clusterlib.ClusterLib,
    ) -> List[clusterlib.PoolUser]:
        """Create pool users."""
        data_key = id(self.pool_users)
        cached_value = cluster_manager.cache.test_data.get(data_key)
        if cached_value:
            return cached_value  # type: ignore

        created_users = helpers.create_pool_users(
            cluster_obj=cluster,
            name_template="test_negative",
            no_of_addr=2,
        )
        cluster_manager.cache.test_data[data_key] = created_users

        # fund source addresses
        helpers.fund_from_faucet(
            created_users[0],
            cluster_obj=cluster,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=600_000_000,
        )

        return created_users

    @pytest.fixture
    def pool_data(self) -> clusterlib.PoolData:
        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{clusterlib.get_rand_str()}",
            pool_pledge=5,
            pool_cost=3,
            pool_margin=0.01,
        )
        return pool_data

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_cert_wrong_vrf(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Generate pool registration certificate using wrong VRF key."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_registration_cert(
                pool_data=pool_data,
                vrf_vkey_file=node_vrf.skey_file,  # skey instead of vkey
                cold_vkey_file=node_cold.vkey_file,
                owner_stake_vkey_files=[pool_users[0].stake.vkey_file],
            )
        assert "Expected: VrfVerificationKey_PraosVRF" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_cert_wrong_cold(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Generate pool registration certificate using wrong Cold key."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_registration_cert(
                pool_data=pool_data,
                vrf_vkey_file=node_vrf.vkey_file,
                cold_vkey_file=node_cold.skey_file,  # skey instead of vkey
                owner_stake_vkey_files=[pool_users[0].stake.vkey_file],
            )
        assert "Expected: StakePoolVerificationKey" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_cert_wrong_stake(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Generate pool registration certificate using wrong stake key."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_registration_cert(
                pool_data=pool_data,
                vrf_vkey_file=node_vrf.vkey_file,
                cold_vkey_file=node_cold.vkey_file,
                owner_stake_vkey_files=[pool_users[0].stake.skey_file],  # skey instead of vkey
            )
        assert "Expected: StakeVerificationKeyShelley" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_missing_cold_skey(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Register pool using transaction with missing Cold skey."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        pool_reg_cert_file = cluster.gen_pool_registration_cert(
            pool_data=pool_data,
            vrf_vkey_file=node_vrf.vkey_file,
            cold_vkey_file=node_cold.vkey_file,
            owner_stake_vkey_files=[pool_users[0].stake.vkey_file],
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[pool_reg_cert_file],
            signing_key_files=[
                pool_users[0].payment.skey_file,
                # missing node_cold.vkey_file
            ],
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.send_tx(src_address=pool_users[0].payment.address, tx_files=tx_files)
        assert "MissingVKeyWitnessesUTXOW" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_missing_payment_skey(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Register pool using transaction with missing payment skey."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        pool_reg_cert_file = cluster.gen_pool_registration_cert(
            pool_data=pool_data,
            vrf_vkey_file=node_vrf.vkey_file,
            cold_vkey_file=node_cold.vkey_file,
            owner_stake_vkey_files=[pool_users[0].stake.vkey_file],
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[pool_reg_cert_file],
            signing_key_files=[
                # missing payment skey file
                node_cold.vkey_file,
            ],
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.send_tx(src_address=pool_users[0].payment.address, tx_files=tx_files)
        assert "Expected one of" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_registration_conflicting_certs(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Send both pool registration and deregistration certificates in single TX."""
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        pool_reg_cert_file = cluster.gen_pool_registration_cert(
            pool_data=pool_data,
            vrf_vkey_file=node_vrf.vkey_file,
            cold_vkey_file=node_cold.vkey_file,
            owner_stake_vkey_files=[pool_users[0].stake.vkey_file],
        )

        pool_dereg_cert_file = cluster.gen_pool_deregistration_cert(
            pool_name=pool_data.pool_name,
            cold_vkey_file=node_cold.vkey_file,
            epoch=cluster.get_last_block_epoch() + 1,
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[pool_reg_cert_file, pool_dereg_cert_file],
            signing_key_files=[
                pool_users[0].payment.vkey_file,
                pool_users[0].payment.skey_file,
                pool_users[0].stake.vkey_file,
                pool_users[0].stake.skey_file,
                node_cold.vkey_file,
                node_cold.skey_file,
            ],
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.send_tx(src_address=pool_users[0].payment.address, tx_files=tx_files)
        assert "TextView type error" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_pool_deregistration_not_registered(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        pool_data: clusterlib.PoolData,
    ):
        """Deregister pool that is not registered."""
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        pool_dereg_cert_file = cluster.gen_pool_deregistration_cert(
            pool_name=pool_data.pool_name,
            cold_vkey_file=node_cold.vkey_file,
            epoch=cluster.get_last_block_epoch() + 2,
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[pool_dereg_cert_file],
            signing_key_files=[pool_users[0].payment.skey_file, node_cold.skey_file],
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.send_tx(src_address=pool_users[0].payment.address, tx_files=tx_files)
        assert "StakePoolNotRegisteredOnKeyPOOL" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_no_name(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Test pool metadata that is missing the 'name' key."""
        temp_template = "test_stake_pool_metadata_no_name"

        pool_metadata = {
            "description": "cardano-node-tests E2E tests",
            "ticker": "IOG1",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert 'key "name" not found' in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_no_description(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Test pool metadata that is missing the 'description' key."""
        temp_template = "test_stake_pool_metadata_no_description"

        pool_metadata = {
            "name": "cardano-node-tests",
            "ticker": "IOG1",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert 'key "description" not found' in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_no_ticker(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Test pool metadata that is missing the 'ticker' key."""
        temp_template = "test_stake_pool_metadata_no_ticker"

        pool_metadata = {
            "name": "cardano-node-tests",
            "description": "cardano-node-tests E2E tests",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert 'key "ticker" not found' in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_no_homepage(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
    ):
        """Test pool metadata that is missing the 'homepage' key."""
        temp_template = "test_stake_pool_metadata_no_homepage"

        pool_metadata = {
            "name": "cardano-node-tests",
            "description": "cardano-node-tests E2E tests",
            "ticker": "IOG1",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert 'key "homepage" not found' in str(excinfo.value)

    @hypothesis.given(pool_name=st.text(min_size=51))
    @hypothesis.settings(deadline=None, suppress_health_check=(hypothesis.HealthCheck.too_slow,))
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_long_name(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        pool_name: str,
    ):
        """Test pool metadata with the 'name' value longer than allowed."""
        temp_template = "test_stake_pool_metadata_long_name"

        pool_metadata = {
            "name": pool_name,
            "description": "cardano-node-tests E2E tests",
            "ticker": "IOG1",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        err_value = str(excinfo.value)
        assert (
            "Stake pool metadata must consist of at most 512 bytes" in err_value
            or '"name" must have at most 50 characters' in err_value
        )

    @hypothesis.given(pool_description=st.text(min_size=256))
    @hypothesis.settings(deadline=None, suppress_health_check=(hypothesis.HealthCheck.too_slow,))
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_long_description(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        pool_description: str,
    ):
        """Test pool metadata with the 'description' value longer than allowed."""
        temp_template = "test_stake_pool_metadata_long_description"

        pool_metadata = {
            "name": "cardano-node-tests",
            "description": pool_description,
            "ticker": "IOG1",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        err_value = str(excinfo.value)
        assert (
            "Stake pool metadata must consist of at most 512 bytes" in err_value
            or '"description" must have at most 255 characters' in err_value
        )

    @hypothesis.given(pool_ticker=st.text())
    @hypothesis.settings(deadline=None, suppress_health_check=(hypothesis.HealthCheck.too_slow,))
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_long_ticker(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        pool_ticker: str,
    ):
        """Test pool metadata with the 'ticker' value longer than allowed."""
        hypothesis.assume(not (3 <= len(pool_ticker) <= 5))

        temp_template = "test_stake_pool_metadata_long_ticker"

        pool_metadata = {
            "name": "cardano-node-tests",
            "description": "cardano-node-tests E2E tests",
            "ticker": pool_ticker,
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert '"ticker" must have at least 3 and at most 5 characters' in str(excinfo.value)

    @hypothesis.given(pool_homepage=st.text(min_size=425))
    @hypothesis.settings(deadline=None, suppress_health_check=(hypothesis.HealthCheck.too_slow,))
    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_metadata_long_homepage(
        self,
        cluster: clusterlib.ClusterLib,
        temp_dir: Path,
        pool_homepage: str,
    ):
        """Test pool metadata with the 'homepage' value longer than allowed."""
        temp_template = "test_stake_pool_metadata_long_homepage"

        pool_metadata = {
            "name": "CND",
            "description": "CND",
            "ticker": "CND",
            "homepage": pool_homepage,
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{temp_template}_registration_metadata.json", pool_metadata
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_metadata_hash(pool_metadata_file)
        assert "Stake pool metadata must consist of at most 512 bytes" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    def test_stake_pool_long_metadata_url(
        self,
        cluster: clusterlib.ClusterLib,
        pool_users: List[clusterlib.PoolUser],
        temp_dir: Path,
    ):
        """Test pool creation with the 'metadata-url' longer than allowed."""
        pool_name = "cardano-node-tests"
        pool_metadata = {
            "name": pool_name,
            "description": "cardano-node-tests E2E tests",
            "ticker": "IOG2",
            "homepage": "https://github.com/input-output-hk/cardano-node-tests",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"{pool_name}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=pool_name,
            pool_pledge=1000,
            pool_cost=15,
            pool_margin=0.2,
            pool_metadata_url=(
                "https://gist.githubusercontent.com/mkoura/328048d6164b9180633c2332653d0af8/raw/"
                "6c25ce8ec489c7126d89be455dffb050995e09fc/cardano_node_tests_pool_metadata.json"
            ),
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create node VRF key pair
        node_vrf = cluster.gen_vrf_key_pair(node_name=pool_data.pool_name)
        # create node cold key pair and counter
        node_cold = cluster.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

        # create stake pool registration cert
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.gen_pool_registration_cert(
                pool_data=pool_data,
                vrf_vkey_file=node_vrf.vkey_file,
                cold_vkey_file=node_cold.vkey_file,
                owner_stake_vkey_files=[p.stake.vkey_file for p in pool_users],
            )
        assert "option --metadata-url: The provided string must have at most 64 characters" in str(
            excinfo.value
        )
