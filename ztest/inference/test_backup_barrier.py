import asyncio

from mote.contracts.inference.backup import BackupBarrierCut, BackupConsistency
from mote.runtime.inference.backup import BackupEpochAuthority, BackupParticipant, classify_backup_cut


def test_missing_caller_cannot_be_promoted_to_application_consistent():
    cut = BackupBarrierCut(
        backup_id="backup-1",
        backup_epoch=4,
        admission_epoch=9,
        required_participants=("caller-a", "caller-b"),
        acknowledged_participants=("caller-a",),
        daemon_checkpoint_verified=True,
        component_digests_verified=True,
    )

    assert cut.missing_participants == ("caller-b",)
    assert classify_backup_cut(cut) is BackupConsistency.DAEMON_CONSISTENT


def test_unverified_component_degrades_cut_to_crash_consistent():
    cut = BackupBarrierCut(
        backup_id="backup-2",
        backup_epoch=5,
        admission_epoch=10,
        required_participants=("caller-a",),
        acknowledged_participants=("caller-a",),
        daemon_checkpoint_verified=True,
        component_digests_verified=False,
    )

    assert classify_backup_cut(cut) is BackupConsistency.CRASH_CONSISTENT


def test_barrier_fences_old_permits_and_requires_every_caller_acknowledgement():
    async def scenario():
        authority = BackupEpochAuthority()

        async def acknowledged(backup_epoch, admission_epoch):
            return (backup_epoch, admission_epoch) == (1, 1)

        async def missing(backup_epoch, admission_epoch):
            return False

        cut = await authority.begin_cut(
            (
                BackupParticipant("caller-a", acknowledged),
                BackupParticipant("caller-b", missing),
            ),
            timeout_seconds=1,
            daemon_checkpoint_verified=True,
            component_digests_verified=True,
        )
        return authority, cut

    authority, cut = asyncio.run(scenario())
    assert authority.current() == (1, 1)
    assert cut.acknowledged_participants == ("caller-a",)
    assert classify_backup_cut(cut) is BackupConsistency.DAEMON_CONSISTENT


def test_barrier_timeout_returns_a_daemon_consistent_cut():
    async def scenario():
        authority = BackupEpochAuthority()

        async def slow(backup_epoch, admission_epoch):
            await asyncio.sleep(1)
            return True

        return await authority.begin_cut(
            (BackupParticipant("slow-caller", slow),),
            timeout_seconds=0.01,
            daemon_checkpoint_verified=True,
            component_digests_verified=True,
        )

    cut = asyncio.run(scenario())
    assert cut.acknowledged_participants == ()
    assert classify_backup_cut(cut) is BackupConsistency.DAEMON_CONSISTENT
