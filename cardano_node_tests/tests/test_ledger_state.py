"""Tests for ledger state."""
import logging
from pathlib import Path

import allure
import pytest
from _pytest.tmpdir import TempdirFactory
from cardano_clusterlib import clusterlib

from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import configuration
from cardano_node_tests.utils import dbsync_utils
from cardano_node_tests.utils import helpers

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def create_temp_dir(tmp_path_factory: TempdirFactory):
    """Create a temporary dir."""
    p = Path(tmp_path_factory.getbasetemp()).joinpath(helpers.get_id_for_mktemp(__file__)).resolve()
    p.mkdir(exist_ok=True, parents=True)
    return p


@pytest.fixture
def temp_dir(create_temp_dir: Path):
    """Change to a temporary dir."""
    with helpers.change_cwd(create_temp_dir):
        yield create_temp_dir


# use the "temp_dir" fixture for all tests automatically
pytestmark = pytest.mark.usefixtures("temp_dir")


LEDGER_STATE_KEYS = (
    "blocksBefore",
    "blocksCurrent",
    "lastEpoch",
    "possibleRewardUpdate",
    "stakeDistrib",
    "stateBefore",
)

def compare_protocol_parameters_between_ledger_and_db(ledger_state_obj, db_sync_record):
    assert ledger_state_obj["minUTxOValue"] == db_sync_record.min_utxo_value
    assert ledger_state_obj["eMax"] == db_sync_record.max_epoch
    assert ledger_state_obj["minFeeB"] == db_sync_record.min_fee_b
    assert ledger_state_obj["tau"] == db_sync_record.treasury_growth_rate
    assert ledger_state_obj["maxBlockBodySize"] == db_sync_record.max_block_size
    assert ledger_state_obj["maxTxSize"] == db_sync_record.max_tx_size
    assert ledger_state_obj["minPoolCost"] == db_sync_record.min_pool_cost
    assert ledger_state_obj["minFeeA"] == db_sync_record.min_fee_a
    assert ledger_state_obj["nOpt"] == db_sync_record.optimal_pool_count
    assert ledger_state_obj["maxBlockHeaderSize"] == db_sync_record.max_bh_size
    assert ledger_state_obj["keyDeposit"] == db_sync_record.key_deposit
    assert ledger_state_obj["poolDeposit"] == db_sync_record.pool_deposit
    assert ledger_state_obj["protocolVersion"]["minor"] == db_sync_record.protocol_minor
    assert ledger_state_obj["protocolVersion"]["major"] == db_sync_record.protocol_major
    assert ledger_state_obj["a0"] == db_sync_record.influence
    assert ledger_state_obj["rho"] == db_sync_record.monetary_expand_rate
    assert ledger_state_obj["decentralisationParam"] == db_sync_record.decentralisation


class TestLedgerState:
    """Basic tests for ledger state."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.skipif(
        bool(configuration.TX_ERA),
        reason="different TX eras doesn't affect this test, pointless to run",
    )
    def test_ledger_state_keys(self, cluster: clusterlib.ClusterLib):
        """Check output of `query ledger-state`."""
        ledger_state = clusterlib_utils.get_ledger_state(cluster_obj=cluster)
        assert tuple(sorted(ledger_state)) == LEDGER_STATE_KEYS

    @pytest.mark.dbsync
    @allure.link(helpers.get_vcs_link())
    @pytest.mark.skipif(
        bool(configuration.TX_ERA),
        reason="different TX eras doesn't affect this test, pointless to run",
    )
    def test_ledger_state_protocol_parameters(self, cluster: clusterlib.ClusterLib):
        """Check output of `query ledger-state` for protocal parameters and compare with values stored in db-sync."""
        ledger_state = clusterlib_utils.get_ledger_state(cluster_obj=cluster)
        protocol_parameters_from_ledger = ledger_state["stateBefore"]["esPrevPp"]

        tip = cluster.get_tip()
        current_epoch = tip["epoch"]
        protocol_parameters_from_db = dbsync_utils.get_protocol_parameters(current_epoch)

        if protocol_parameters_from_db:
            compare_protocol_parameters_between_ledger_and_db(protocol_parameters_from_ledger, protocol_parameters_from_db)
