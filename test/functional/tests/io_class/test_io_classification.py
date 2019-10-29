#
# Copyright(c) 2019 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#

import random

import pytest

from test_tools import fs_utils
from test_tools.dd import Dd
from test_tools.disk_utils import Filesystem
from test_tools.fio.fio import Fio
from test_tools.fio.fio_param import ReadWrite, IoEngine
from test_utils.filesystem.file import File
from test_utils.os_utils import sync, Udev
from storage_devices.disk import DiskType, DiskTypeSet, DiskTypeLowerThan
from .io_class_common import *


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_ioclass_lba():
    """Write data to random lba and check if it is cached according to range
    defined in ioclass rule"""
    cache, core = prepare()
    ioclass_id = 1
    min_cached_lba = 56
    max_cached_lba = 200
    iterations = 100
    dd_size = Size(1, Unit.Blocks512)
    dd_count = 1

    # Prepare ioclass config
    ioclass_config.add_ioclass(
        ioclass_id=ioclass_id,
        eviction_priority=1,
        allocation=True,
        rule=f"lba:ge:{min_cached_lba}&lba:le:{max_cached_lba}&done",
        ioclass_config_path=ioclass_config_path,
    )

    # Prepare cache for test
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    cache.flush_cache()

    # Check if lbas from defined range are cached
    dirty_count = 0
    # '8' step is set to prevent writing cache line more than once
    TestRun.LOGGER.info(f"Writing to one sector in each cache line from range.")
    for lba in range(min_cached_lba, max_cached_lba, 8):
        dd = (
            Dd()
            .input("/dev/zero")
            .output(f"{core.system_path}")
            .count(dd_count)
            .block_size(dd_size)
            .seek(lba)
        )
        dd.run()
        sync()
        dirty_count += 1

        stats = cache.get_cache_statistics(io_class_id=ioclass_id)
        assert (
            stats["dirty"].get_value(Unit.Blocks4096) == dirty_count
        ), f"LBA {lba} not cached"

    cache.flush_cache()

    # Check if lba outside of defined range are not cached
    TestRun.LOGGER.info(f"Writing to random sectors outside of cached range.")
    for i in range(iterations):
        rand_lba = random.randrange(2000)
        if min_cached_lba <= rand_lba <= max_cached_lba:
            continue
        dd = (
            Dd()
            .input("/dev/zero")
            .output(f"{core.system_path}")
            .count(dd_count)
            .block_size(dd_size)
            .seek(rand_lba)
        )
        dd.run()
        sync()

        stats = cache.get_cache_statistics(io_class_id=ioclass_id)
        assert (
            stats["dirty"].get_value(Unit.Blocks4096) == 0
        ), f"Inappropriately cached lba: {rand_lba}"


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_ioclass_request_size():
    cache, core = prepare()

    ioclass_id = 1
    iterations = 100

    ioclass_config.add_ioclass(
        ioclass_id=ioclass_id,
        eviction_priority=1,
        allocation=True,
        rule=f"request_size:ge:8192&request_size:le:16384&done",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    Udev.disable()

    # Check if requests with appropriate size are cached
    TestRun.LOGGER.info(
        f"Check if requests with size within defined range are cached"
    )
    cached_req_sizes = [Size(2, Unit.Blocks4096), Size(4, Unit.Blocks4096)]
    for i in range(iterations):
        cache.flush_cache()
        req_size = random.choice(cached_req_sizes)
        dd = (
            Dd()
            .input("/dev/zero")
            .output(core.system_path)
            .count(1)
            .block_size(req_size)
            .oflag("direct")
        )
        dd.run()
        stats = cache.get_cache_statistics(io_class_id=ioclass_id)
        assert (
            stats["dirty"].get_value(Unit.Blocks4096)
            == req_size.value / Unit.Blocks4096.value
        )

    cache.flush_cache()

    # Check if requests with inappropriate size are not cached
    TestRun.LOGGER.info(
        f"Check if requests with size outside defined range are not cached"
    )
    not_cached_req_sizes = [
        Size(1, Unit.Blocks4096),
        Size(8, Unit.Blocks4096),
        Size(16, Unit.Blocks4096),
    ]
    for i in range(iterations):
        req_size = random.choice(not_cached_req_sizes)
        dd = (
            Dd()
            .input("/dev/zero")
            .output(core.system_path)
            .count(1)
            .block_size(req_size)
            .oflag("direct")
        )
        dd.run()
        stats = cache.get_cache_statistics(io_class_id=ioclass_id)
        assert stats["dirty"].get_value(Unit.Blocks4096) == 0


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrize("filesystem", list(Filesystem) + [False])
def test_ioclass_direct(filesystem):
    """
    Perform buffered/direct IO to/from files or raw block device.
    Data from buffered IO should be cached.
    Data from buffered IO should not be cached and if performed to/from already cached data
    should cause reclassification to unclassified IO class.
    """
    cache, core = prepare()
    Udev.disable()

    ioclass_id = 1
    io_size = Size(random.randint(1000, 2000), Unit.Blocks4096)

    # direct IO class
    ioclass_config.add_ioclass(
        ioclass_id=ioclass_id,
        eviction_priority=1,
        allocation=True,
        rule="direct",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    fio = (
        Fio().create_command()
             .io_engine(IoEngine.libaio)
             .size(io_size)
             .offset(io_size)
             .read_write(ReadWrite.write)
             .target(f"{mountpoint}/tmp_file" if filesystem else core.system_path)
    )

    if filesystem:
        TestRun.LOGGER.info(
            f"Preparing {filesystem.name} filesystem and mounting {core.system_path} at"
            f" {mountpoint}"
        )
        core.create_filesystem(filesystem)
        core.mount(mountpoint)
        sync()
    else:
        TestRun.LOGGER.info("Testing on raw exported object")

    base_occupancy = cache.get_cache_statistics(io_class_id=ioclass_id)["occupancy"]

    TestRun.LOGGER.info(f"Buffered writes to {'file' if filesystem else 'device'}")
    fio.run()
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=ioclass_id)["occupancy"]
    assert new_occupancy == base_occupancy, \
        "Buffered writes were cached!\n" \
        f"Expected: {base_occupancy}, actual: {new_occupancy}"

    TestRun.LOGGER.info(f"Direct writes to {'file' if filesystem else 'device'}")
    fio.direct()
    fio.run()
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=ioclass_id)["occupancy"]
    assert new_occupancy == base_occupancy + io_size, \
        "Wrong number of direct writes was cached!\n" \
        f"Expected: {base_occupancy + io_size}, actual: {new_occupancy}"

    TestRun.LOGGER.info(f"Buffered reads from {'file' if filesystem else 'device'}")
    fio.remove_param("readwrite").remove_param("direct")
    fio.read_write(ReadWrite.read)
    fio.run()
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=ioclass_id)["occupancy"]
    assert new_occupancy == base_occupancy, \
        "Buffered reads did not cause reclassification!" \
        f"Expected occupancy: {base_occupancy}, actual: {new_occupancy}"

    TestRun.LOGGER.info(f"Direct reads from {'file' if filesystem else 'device'}")
    fio.direct()
    fio.run()
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=ioclass_id)["occupancy"]
    assert new_occupancy == base_occupancy + io_size, \
        "Wrong number of direct reads was cached!\n" \
        f"Expected: {base_occupancy + io_size}, actual: {new_occupancy}"


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrize("filesystem", Filesystem)
def test_ioclass_metadata(filesystem):
    """
    Perform operations on files that cause metadata update.
    Determine if every such operation results in increased writes to cached metadata.
    Exact values may not be tested as each file system has different metadata structure.
    """
    cache, core = prepare()
    Udev.disable()

    ioclass_id = random.randint(1, ioclass_config.MAX_IO_CLASS_ID)
    # metadata IO class
    ioclass_config.add_ioclass(
        ioclass_id=ioclass_id,
        eviction_priority=1,
        allocation=True,
        rule="metadata&done",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    TestRun.LOGGER.info(f"Preparing {filesystem.name} filesystem "
                        f"and mounting {core.system_path} at {mountpoint}")
    core.create_filesystem(filesystem)
    core.mount(mountpoint)
    sync()

    requests_to_metadata_before = cache.get_cache_statistics(
        io_class_id=ioclass_id)["write total"]
    TestRun.LOGGER.info("Creating 20 test files")
    files = []
    for i in range(1, 21):
        file_path = f"{mountpoint}/test_file_{i}"
        dd = (
            Dd()
            .input("/dev/urandom")
            .output(file_path)
            .count(random.randint(5, 50))
            .block_size(Size(1, Unit.MebiByte))
            .oflag("sync")
        )
        dd.run()
        files.append(File(file_path))

    TestRun.LOGGER.info("Checking requests to metadata")
    requests_to_metadata_after = cache.get_cache_statistics(
        io_class_id=ioclass_id)["write total"]
    if requests_to_metadata_after == requests_to_metadata_before:
        pytest.xfail("No requests to metadata while creating files!")

    requests_to_metadata_before = requests_to_metadata_after
    TestRun.LOGGER.info("Renaming all test files")
    for file in files:
        file.move(f"{file.full_path}_renamed")
    sync()

    TestRun.LOGGER.info("Checking requests to metadata")
    requests_to_metadata_after = cache.get_cache_statistics(
        io_class_id=ioclass_id)["write total"]
    if requests_to_metadata_after == requests_to_metadata_before:
        pytest.xfail("No requests to metadata while renaming files!")

    requests_to_metadata_before = requests_to_metadata_after
    test_dir_path = f"{mountpoint}/test_dir"
    TestRun.LOGGER.info(f"Creating directory {test_dir_path}")
    fs_utils.create_directory(path=test_dir_path)

    TestRun.LOGGER.info(f"Moving test files into {test_dir_path}")
    for file in files:
        file.move(test_dir_path)
    sync()

    TestRun.LOGGER.info("Checking requests to metadata")
    requests_to_metadata_after = cache.get_cache_statistics(
        io_class_id=ioclass_id)["write total"]
    if requests_to_metadata_after == requests_to_metadata_before:
        pytest.xfail("No requests to metadata while moving files!")

    TestRun.LOGGER.info(f"Removing {test_dir_path}")
    fs_utils.remove(path=test_dir_path, force=True, recursive=True)

    TestRun.LOGGER.info("Checking requests to metadata")
    requests_to_metadata_after = cache.get_cache_statistics(
        io_class_id=ioclass_id)["write total"]
    if requests_to_metadata_after == requests_to_metadata_before:
        pytest.xfail("No requests to metadata while deleting directory with files!")


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrize("filesystem", Filesystem)
def test_ioclass_id_as_condition(filesystem):
    """
    Load config in which IO class ids are used as conditions in other IO class definitions.
    Check if performed IO is properly classified.
    """
    cache, core = prepare()
    Udev.disable()

    base_dir_path = f"{mountpoint}/base_dir"
    ioclass_file_size = Size(random.randint(25, 50), Unit.MebiByte)
    ioclass_file_size_bytes = int(ioclass_file_size.get_value(Unit.Byte))

    # directory condition
    ioclass_config.add_ioclass(
        ioclass_id=1,
        eviction_priority=1,
        allocation=True,
        rule=f"directory:{base_dir_path}",
        ioclass_config_path=ioclass_config_path,
    )
    # file size condition
    ioclass_config.add_ioclass(
        ioclass_id=2,
        eviction_priority=1,
        allocation=True,
        rule=f"file_size:eq:{ioclass_file_size_bytes}",
        ioclass_config_path=ioclass_config_path,
    )
    # direct condition
    ioclass_config.add_ioclass(
        ioclass_id=3,
        eviction_priority=1,
        allocation=True,
        rule="direct",
        ioclass_config_path=ioclass_config_path,
    )
    # IO class 1 OR 2 condition
    ioclass_config.add_ioclass(
        ioclass_id=4,
        eviction_priority=1,
        allocation=True,
        rule="io_class:1|io_class:2",
        ioclass_config_path=ioclass_config_path,
    )
    # IO class 4 AND file size condition (same as IO class 2)
    ioclass_config.add_ioclass(
        ioclass_id=5,
        eviction_priority=1,
        allocation=True,
        rule=f"io_class:4&file_size:eq:{ioclass_file_size_bytes}",
        ioclass_config_path=ioclass_config_path,
    )
    # IO class 3 condition
    ioclass_config.add_ioclass(
        ioclass_id=6,
        eviction_priority=1,
        allocation=True,
        rule="io_class:3",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    TestRun.LOGGER.info(f"Preparing {filesystem.name} filesystem "
                        f"and mounting {core.system_path} at {mountpoint}")
    core.create_filesystem(filesystem)
    core.mount(mountpoint)
    fs_utils.create_directory(base_dir_path)
    sync()

    # IO fulfilling IO class 1 condition (and not IO class 2)
    # Should be classified as IO class 4
    base_occupancy = cache.get_cache_statistics(io_class_id=4)["occupancy"]
    non_ioclass_file_size = Size(random.randrange(1, 25), Unit.MebiByte)
    (Fio().create_command()
          .io_engine(IoEngine.libaio)
          .size(non_ioclass_file_size)
          .read_write(ReadWrite.write)
          .target(f"{base_dir_path}/test_file_1")
          .run())
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=4)["occupancy"]

    assert new_occupancy == base_occupancy + non_ioclass_file_size, \
        "Writes were not properly cached!\n" \
        f"Expected: {base_occupancy + non_ioclass_file_size}, actual: {new_occupancy}"

    # IO fulfilling IO class 2 condition (and not IO class 1)
    # Should be classified as IO class 5
    base_occupancy = cache.get_cache_statistics(io_class_id=5)["occupancy"]
    (Fio().create_command()
          .io_engine(IoEngine.libaio)
          .size(ioclass_file_size)
          .read_write(ReadWrite.write)
          .target(f"{mountpoint}/test_file_2")
          .run())
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=5)["occupancy"]

    assert new_occupancy == base_occupancy + ioclass_file_size, \
        "Writes were not properly cached!\n" \
        f"Expected: {base_occupancy + ioclass_file_size}, actual: {new_occupancy}"

    # IO fulfilling IO class 1 and 2 conditions
    # Should be classified as IO class 5
    base_occupancy = new_occupancy
    (Fio().create_command()
          .io_engine(IoEngine.libaio)
          .size(ioclass_file_size)
          .read_write(ReadWrite.write)
          .target(f"{base_dir_path}/test_file_3")
          .run())
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=5)["occupancy"]

    assert new_occupancy == base_occupancy + ioclass_file_size, \
        "Writes were not properly cached!\n" \
        f"Expected: {base_occupancy + ioclass_file_size}, actual: {new_occupancy}"

    # Same IO but direct
    # Should be classified as IO class 6
    base_occupancy = cache.get_cache_statistics(io_class_id=6)["occupancy"]
    (Fio().create_command()
          .io_engine(IoEngine.libaio)
          .size(ioclass_file_size)
          .read_write(ReadWrite.write)
          .target(f"{base_dir_path}/test_file_3")
          .direct()
          .run())
    sync()
    new_occupancy = cache.get_cache_statistics(io_class_id=6)["occupancy"]

    assert new_occupancy == base_occupancy + ioclass_file_size, \
        "Writes were not properly cached!\n" \
        f"Expected: {base_occupancy + ioclass_file_size}, actual: {new_occupancy}"


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrize("filesystem", Filesystem)
def test_ioclass_conditions_or(filesystem):
    """
    Load config with IO class combining 5 contradicting conditions connected by OR operator.
    Check if every IO fulfilling one condition is classified properly.
    """
    cache, core = prepare()
    Udev.disable()

    # directories OR condition
    ioclass_config.add_ioclass(
        ioclass_id=1,
        eviction_priority=1,
        allocation=True,
        rule=f"directory:{mountpoint}/dir1|directory:{mountpoint}/dir2|directory:"
             f"{mountpoint}/dir3|directory:{mountpoint}/dir4|directory:{mountpoint}/dir5",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    TestRun.LOGGER.info(f"Preparing {filesystem.name} filesystem "
                        f"and mounting {core.system_path} at {mountpoint}")
    core.create_filesystem(filesystem)
    core.mount(mountpoint)
    for i in range(1, 6):
        fs_utils.create_directory(f"{mountpoint}/dir{i}")
    sync()

    # Perform IO fulfilling each condition and check if occupancy raises
    for i in range(1, 6):
        file_size = Size(random.randint(25, 50), Unit.MebiByte)
        base_occupancy = cache.get_cache_statistics(io_class_id=1)["occupancy"]
        (Fio().create_command()
              .io_engine(IoEngine.libaio)
              .size(file_size)
              .read_write(ReadWrite.write)
              .target(f"{mountpoint}/dir{i}/test_file")
              .run())
        sync()
        new_occupancy = cache.get_cache_statistics(io_class_id=1)["occupancy"]

        assert new_occupancy == base_occupancy + file_size, \
            "Occupancy has not increased correctly!\n" \
            f"Expected: {base_occupancy + file_size}, actual: {new_occupancy}"


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrize("filesystem", Filesystem)
def test_ioclass_conditions_and(filesystem):
    """
    Load config with IO class combining 5 conditions contradicting at least one other condition
    connected by AND operator.
    Check if every IO fulfilling one of the conditions is not classified.
    """
    cache, core = prepare()
    Udev.disable()
    file_size = Size(random.randint(25, 50), Unit.MebiByte)
    file_size_bytes = int(file_size.get_value(Unit.Byte))

    # directories OR condition
    ioclass_config.add_ioclass(
        ioclass_id=1,
        eviction_priority=1,
        allocation=True,
        rule=f"file_size:gt:{file_size_bytes}&file_size:lt:{file_size_bytes}&"
             f"file_size:ge:{file_size_bytes}&file_size:le:{file_size_bytes}&"
             f"file_size:eq:{file_size_bytes}",
        ioclass_config_path=ioclass_config_path,
    )
    casadm.load_io_classes(cache_id=cache.cache_id, file=ioclass_config_path)

    TestRun.LOGGER.info(f"Preparing {filesystem.name} filesystem "
                        f"and mounting {core.system_path} at {mountpoint}")
    core.create_filesystem(filesystem)
    core.mount(mountpoint)
    sync()

    base_occupancy = cache.get_cache_statistics(io_class_id=1)["occupancy"]
    # Perform IO
    for size in [file_size, file_size + Size(1, Unit.MebiByte), file_size - Size(1, Unit.MebiByte)]:
        (Fio().create_command()
              .io_engine(IoEngine.libaio)
              .size(size)
              .read_write(ReadWrite.write)
              .target(f"{mountpoint}/test_file")
              .run())
        sync()
        new_occupancy = cache.get_cache_statistics(io_class_id=1)["occupancy"]

        assert new_occupancy == base_occupancy, \
            "Unexpected occupancy increase!\n" \
            f"Expected: {base_occupancy}, actual: {new_occupancy}"
