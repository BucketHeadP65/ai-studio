"""Two spill stores in one process never read each other's returns."""

from harness.adapters.environment import ShortHandles, spill_store


async def test_two_spill_stores_keep_their_own_returns() -> None:
    first, second = spill_store(), spill_store()

    first_alias = await first.write("call-a", b"from A")
    second_alias = await second.write("call-b", b"from B")

    assert first_alias != second_alias
    assert await first.read(first_alias) == b"from A"
    assert await second.read(second_alias) == b"from B"


def test_every_spill_store_shares_the_process_folder_its_cleanup_covers() -> None:
    first, second = spill_store(), spill_store()

    assert isinstance(first, ShortHandles)
    assert isinstance(second, ShortHandles)
    assert first.inner.base_dir == second.inner.base_dir
