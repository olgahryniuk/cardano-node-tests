#! /usr/bin/env nix-shell
#! nix-shell -i bash --keep LOG_FILEPATH --keep ENVIRONMENT --keep POSTGRES_DIR --keep PGUSER -p glibcLocales postgresql lsof procps
# shellcheck shell=bash

cd cardano-db-sync

export PGPASSFILE=config/pgpass-$ENVIRONMENT

nix-build -A cardano-db-sync -o db-sync-node

export DbSyncAbortOnPanic=1

if [ "$ENVIRONMENT" = "shelley_qa" ];
then
    PGPASSFILE=$PGPASSFILE db-sync-node/bin/cardano-db-sync --config config/shelley-qa-config.json --socket-path ../cardano-node/db-${ENVIRONMENT}/node.socket --schema-dir schema/ --state-dir ledger-state/${ENVIRONMENT} >> ${LOG_FILEPATH} &
else
    PGPASSFILE=$PGPASSFILE db-sync-node/bin/cardano-db-sync --config config/${ENVIRONMENT}-config.yaml --socket-path ../cardano-node/db-${ENVIRONMENT}/node.socket --schema-dir schema/ --state-dir ledger-state/${ENVIRONMENT} >> ${LOG_FILEPATH} &
fi