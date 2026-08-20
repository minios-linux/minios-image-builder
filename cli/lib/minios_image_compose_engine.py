#!/usr/bin/env python3
# minios-image-compose isolated helper engine (Python 3.6+, stdlib only).
#
# Extracted from the minios-image-compose bash adapter so the security-critical operations
# (input identity recording, in-place graft snapshotting, overlay and capture
# handling, and post-write verification) live in a real Python module instead of
# an embedded here-doc string. minios-image-compose runs it via: env -i python3 -I engine <cmd>.
from __future__ import print_function

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import posixpath
import re
import signal
import stat
import sys
import time
import zlib


class AdapterError(Exception):
    pass


def fail(message):
    raise AdapterError(message)


def open_absolute_directory(path):
    path_bytes = os.path.abspath(os.fsencode(path))
    descriptor = os.open(b"/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path_bytes.split(b"/"):
            if not component:
                continue
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def canonical_directory(path):
    canonical = os.path.realpath(os.path.abspath(os.fsencode(path)))
    descriptor = open_absolute_directory(canonical)
    return canonical, descriptor, os.fstat(descriptor)


def stable_snapshot(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_uid, metadata.st_gid, metadata.st_rdev, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def open_final_nofollow(path):
    absolute = os.path.abspath(os.fsencode(path))
    parent = os.path.realpath(os.path.dirname(absolute))
    parent_fd = open_absolute_directory(parent)
    try:
        metadata = os.stat(os.path.basename(absolute), dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            return parent_fd, os.path.basename(absolute), metadata, None
        descriptor = os.open(os.path.basename(absolute), os.O_RDONLY | os.O_NOFOLLOW,
                             dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
                metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)):
            os.close(descriptor)
            fail("input identity changed while opening")
        return parent_fd, os.path.basename(absolute), opened, descriptor
    except Exception:
        os.close(parent_fd)
        raise


def hash_fd(descriptor, expected=None):
    before = os.fstat(descriptor)
    if expected is not None and stable_snapshot(before) != stable_snapshot(expected):
        fail("file changed before hashing")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    if stable_snapshot(os.fstat(descriptor)) != stable_snapshot(before):
        fail("file changed while hashing")
    return digest.hexdigest()


def hash_file(path):
    parent_fd, _name, metadata, descriptor = open_final_nofollow(path)
    try:
        if descriptor is None or not stat.S_ISREG(metadata.st_mode):
            fail("hash input is not a non-symlink regular file")
        return metadata.st_size, hash_fd(descriptor, metadata)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("duplicate JSON field: {}".format(key))
        value[key] = item
    return value


def reject_json_constant(value):
    fail("invalid JSON numeric constant: {}".format(value))


def load_json(path):
    parent_fd, _name, metadata, descriptor = open_final_nofollow(path)
    try:
        if descriptor is None or not stat.S_ISREG(metadata.st_mode):
            fail("JSON input is not a non-symlink regular file")
        raw = b""
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            raw += block
        if stable_snapshot(os.fstat(descriptor)) != stable_snapshot(metadata):
            fail("JSON input changed while being read")
        return json.loads(raw.decode("utf-8", "strict"),
                          object_pairs_hook=strict_json_object,
                          parse_constant=reject_json_constant)
    except (UnicodeError, ValueError) as error:
        fail("invalid JSON: {}".format(error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def validate_json_object(path):
    value = load_json(path)
    if not isinstance(value, dict):
        fail("JSON top-level value must be an object")


def snapshot_file(source, destination):
    parent_fd, _name, metadata, descriptor = open_final_nofollow(source)
    try:
        if descriptor is None or not stat.S_ISREG(metadata.st_mode):
            fail("snapshot input is not a non-symlink regular file")
        output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                offset = 0
                while offset < len(block):
                    offset += os.write(output_fd, block[offset:])
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        if stable_snapshot(os.fstat(descriptor)) != stable_snapshot(metadata):
            fail("snapshot input changed while being copied")
        os.chmod(destination, stat.S_IMODE(metadata.st_mode))
        os.utime(destination, ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                 follow_symlinks=False)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def rename_noreplace(source_fd, source, target_fd, target):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                              ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(source_fd, source, target_fd, target, 1) == 0:
            return "rename"
        number = ctypes.get_errno()
        if number not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(number, os.strerror(number))
    os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd,
            follow_symlinks=False)
    return "link"


def prepare_output(parent_path, target, overwrite):
    canonical, parent_fd, parent_metadata = canonical_directory(parent_path)
    target_bytes = os.fsencode(target)
    try:
        try:
            target_metadata = os.stat(target_bytes, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(target_metadata.st_mode):
                fail("output target is a directory")
            if overwrite != "true":
                fail("output already exists and --overwrite was not supplied")
            target_state = "present:{}:{}:{}".format(
                target_metadata.st_dev, target_metadata.st_ino, stat.S_IFMT(target_metadata.st_mode))
        except FileNotFoundError:
            target_state = "absent"
        work_name = None
        for _ in range(128):
            candidate = b".minios-image-compose-output." + os.urandom(16).hex().encode("ascii")
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
                work_name = candidate
                break
            except FileExistsError:
                pass
        if work_name is None:
            fail("cannot create private same-filesystem ISO directory")
        work_fd = os.open(work_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=parent_fd)
        os.fchmod(work_fd, 0o700)
        work_metadata = os.fstat(work_fd)
        values = (
            os.fsdecode(canonical), str(parent_metadata.st_dev), str(parent_metadata.st_ino),
            os.fsdecode(work_name), str(work_metadata.st_dev), str(work_metadata.st_ino),
            target_state,
        )
        for value in values:
            print(value)
        os.close(work_fd)
    finally:
        os.close(parent_fd)


def check_output_fds(parent_fd_number, work_fd_number, parent_dev, parent_ino,
                     work_name, work_dev, work_ino):
    parent_fd = os.dup(int(parent_fd_number))
    work_fd = os.dup(int(work_fd_number))
    try:
        parent = os.fstat(parent_fd)
        work = os.fstat(work_fd)
        named = os.stat(os.fsencode(work_name), dir_fd=parent_fd, follow_symlinks=False)
        if (parent.st_dev, parent.st_ino) != (int(parent_dev), int(parent_ino)):
            fail("retained output parent identity mismatch")
        if (work.st_dev, work.st_ino) != (int(work_dev), int(work_ino)):
            fail("retained ISO directory identity mismatch")
        if (named.st_dev, named.st_ino) != (work.st_dev, work.st_ino):
            fail("ISO directory name was replaced")
    finally:
        os.close(work_fd)
        os.close(parent_fd)


def read_nul(path):
    with open(path, "rb") as stream:
        data = stream.read()
    if not data:
        return []
    if not data.endswith(b"\0"):
        fail("truncated input path list")
    return data[:-1].split(b"\0")


def target_set_identity(targets):
    if any(not isinstance(target, str) or not target for target in targets):
        fail("attested target set contains an invalid path")
    if len(set(targets)) != len(targets):
        fail("attested target set contains duplicate paths")
    digest = hashlib.sha256()
    digest.update(b"minios-image-target-set-v1\0")
    for target in sorted(targets):
        digest.update(target.encode("utf-8", "strict"))
        digest.update(b"\0")
    return len(targets), digest.hexdigest()


def target_set_file(path, stride_text):
    stride = int(stride_text)
    values = read_nul(path)
    if stride not in (1, 2) or len(values) % stride:
        fail("target-set input has an invalid stride")
    targets = [values[index].decode("utf-8", "strict")
               for index in range(0, len(values), stride)]
    count, digest = target_set_identity(targets)
    print(count)
    print(digest)


def input_record(path):
    text_path = os.fsdecode(path)
    parent_fd, name, metadata, descriptor = open_final_nofollow(text_path)
    try:
        base = {
            "path": text_path,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IFMT(metadata.st_mode),
            "permissions": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }
        if stat.S_ISREG(metadata.st_mode):
            base["kind"] = "regular"
            base["sha256"] = hash_fd(descriptor, metadata)
        elif stat.S_ISLNK(metadata.st_mode):
            base["kind"] = "symlink"
            target = os.readlink(name, dir_fd=parent_fd)
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stable_snapshot(after) != stable_snapshot(metadata):
                fail("symbolic-link input changed while being read")
            base["target_sha256"] = hashlib.sha256(os.fsencode(target)).hexdigest()
        else:
            fail("build input is not a regular file or symbolic link")
        return base
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def record_inputs(list_path, output_path):
    records = []
    seen = set()
    for path in read_nul(list_path):
        if path in seen:
            continue
        seen.add(path)
        records.append(input_record(path))
    with open(output_path, "x", encoding="utf-8") as stream:
        json.dump(records, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def verify_inputs(records_path, required_list):
    records = load_json(records_path)
    if not isinstance(records, list):
        fail("input identity record is invalid")
    by_path = {os.fsencode(record["path"]): record for record in records}
    if required_list:
        missing = [path for path in read_nul(required_list) if path not in by_path]
        if missing:
            fail("final graft source was not present in the original input snapshot")
    for record in records:
        current = input_record(os.fsencode(record["path"]))
        if current != record:
            fail("build input identity or digest changed: {}".format(repr(record["path"])))


def module_fingerprint_from_records(records_path, module_list):
    records = load_json(records_path)
    if not isinstance(records, list):
        fail("input identity record is invalid")
    by_path = {os.fsencode(record["path"]): record for record in records}
    modules = []
    names = set()
    for path in read_nul(module_list):
        record = by_path.get(path)
        if record is None or record.get("kind") != "regular":
            fail("source module is absent from the input identity record")
        name = os.path.basename(path)
        if name in names:
            fail("source module basenames are not unique")
        names.add(name)
        modules.append((name, record["size"], record["sha256"]))
    if not modules:
        fail("source module fingerprint has no modules")
    modules.sort(key=lambda item: (
        int(re.match(br"^[0-9]+", item[0]).group())
        if re.match(br"^[0-9]+", item[0]) else 0,
        item[0]), reverse=True)
    digest = hashlib.sha256()
    digest.update(b"minios-base-modules-v2\0")
    for name, size, module_digest in modules:
        digest.update(name)
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(module_digest.encode("ascii"))
        digest.update(b"\0")
    print(digest.hexdigest())


def metadata_matches_record(metadata, record):
    return (metadata.st_dev == record["device"] and metadata.st_ino == record["inode"] and
            stat.S_IFMT(metadata.st_mode) == record["mode"] and
            stat.S_IMODE(metadata.st_mode) == record["permissions"] and
            metadata.st_size == record["size"] and metadata.st_mtime_ns == record["mtime_ns"] and
            metadata.st_ctime_ns == record["ctime_ns"] and metadata.st_uid == record["uid"] and
            metadata.st_gid == record["gid"])


def snapshot_inputs(records_path, source_list, snapshot_directory, mapping_path):
    records = load_json(records_path)
    if not isinstance(records, list):
        fail("input identity record is invalid")
    by_path = {os.fsencode(record["path"]): record for record in records}
    os.mkdir(snapshot_directory, 0o700)
    mappings = []
    for index, path in enumerate(read_nul(source_list)):
        record = by_path.get(path)
        if record is None:
            fail("snapshot input is absent from the identity record")
        parent_fd, name, metadata, descriptor = open_final_nofollow(os.fsdecode(path))
        try:
            if not metadata_matches_record(metadata, record):
                fail("snapshot input identity changed")
            destination = os.path.join(os.fsencode(snapshot_directory),
                                       ("input-{:08d}".format(index)).encode("ascii"))
            if record["kind"] == "symlink":
                target = os.readlink(name, dir_fd=parent_fd)
                after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (not metadata_matches_record(after, record) or
                        hashlib.sha256(os.fsencode(target)).hexdigest() != record["target_sha256"]):
                    fail("symbolic-link snapshot input changed")
                os.symlink(target, destination)
                os.utime(destination, ns=(record["mtime_ns"], record["mtime_ns"]),
                         follow_symlinks=False)
                snapshot = destination
            elif record["kind"] == "regular" and descriptor is not None:
                if hash_fd(descriptor, metadata) != record["sha256"]:
                    fail("snapshot input digest changed")
                snapshot = destination
                cloned = False
                clone_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    try:
                        fcntl.ioctl(clone_fd, 0x40049409, descriptor)
                        cloned = True
                        os.fsync(clone_fd)
                    except OSError as error:
                        if error.errno not in (errno.EXDEV, errno.EINVAL, errno.ENOTTY,
                                              errno.EOPNOTSUPP, errno.ENOSYS):
                            raise
                finally:
                    os.close(clone_fd)
                if not cloned:
                    # Never byte-copy inputs into the private job directory. MiniOS
                    # often runs with little RAM and the job directory is tmpfs, so
                    # duplicating the source modules would risk OOM. A reflink (CoW,
                    # no data duplication) is used when the filesystem supports it;
                    # otherwise the original path is grafted directly. Integrity is
                    # still guaranteed: the content was digest-verified above and the
                    # post-write ISO byte verification fails closed on any mutation.
                    os.unlink(destination)
                    snapshot = path
                if snapshot != path:
                    os.chmod(destination, record["permissions"])
                    os.utime(destination, ns=(record["mtime_ns"], record["mtime_ns"]),
                             follow_symlinks=False)
                    snapshot_size, snapshot_digest = hash_file(os.fsdecode(snapshot))
                    if snapshot_size != record["size"] or snapshot_digest != record["sha256"]:
                        fail("private input snapshot does not match its recorded digest")
                    snapshot_metadata = os.stat(snapshot, follow_symlinks=False)
                    if (stat.S_IMODE(snapshot_metadata.st_mode) != record["permissions"] or
                            snapshot_metadata.st_mtime_ns != record["mtime_ns"]):
                        fail("private input snapshot does not preserve source metadata")
            else:
                fail("unsupported snapshot input type")
            mappings.append((path, snapshot))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)
    with open(mapping_path, "xb") as stream:
        for source, snapshot in mappings:
            stream.write(source + b"\0" + snapshot + b"\0")


def snapshot_fallback_size(records_path, source_list):
    # Inputs are never byte-copied into the private job directory (see
    # snapshot_inputs): a reflink shares blocks without duplication and the
    # non-reflink path grafts the original file in place. No fallback copy
    # space is therefore reserved, which keeps low-RAM MiniOS sessions safe.
    load_json(records_path)
    read_nul(source_list)
    print(0)


def read_stable_regular(path, maximum=None):
    parent_fd, _name, metadata, descriptor = open_final_nofollow(path)
    try:
        if descriptor is None or not stat.S_ISREG(metadata.st_mode):
            fail("input is not a non-symlink regular file")
        if maximum is not None and metadata.st_size > maximum:
            fail("input exceeds the supported size")
        blocks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
        if stable_snapshot(os.fstat(descriptor)) != stable_snapshot(metadata):
            fail("input changed while being read")
        return metadata, b"".join(blocks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


KERNEL_ARGUMENT_FORBIDDEN = set("\\\"'`$;&|<>(){}[]*?!#")


def validate_kernel_arguments(value):
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        fail("kernel arguments are not valid UTF-8")
    if not encoded or len(encoded) > 4096:
        fail("kernel arguments have an invalid UTF-8 byte length")
    for character in value:
        if (not character.isprintable() or (character.isspace() and character != " ") or
                character in KERNEL_ARGUMENT_FORBIDDEN):
            fail("kernel arguments contain syntax unsafe for a bootloader command line")
    if value[0] == " " or value[-1] == " ":
        fail("kernel arguments must not begin or end with a space")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def png_metadata(path):
    metadata, data = read_stable_regular(path, 16 * 1024 * 1024)
    if len(data) < 57 or data[:8] != b"\x89PNG\r\n\x1a\n":
        fail("boot background has an invalid PNG signature or structure")
    offset = 8
    chunk_index = 0
    width = height = bit_depth = color_type = interlace = None
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    idat_closed = False
    idat_bytes = 0
    idat_parts = []
    seen_iend = False
    while offset < len(data):
        if len(data) - offset < 12:
            fail("PNG has a truncated chunk header")
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            fail("PNG chunk exceeds the input bounds")
        if (len(chunk_type) != 4 or
                any(value not in range(ord("A"), ord("Z") + 1) and
                    value not in range(ord("a"), ord("z") + 1) for value in chunk_type) or
                not chr(chunk_type[2]).isupper()):
            fail("PNG chunk type is invalid")
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length:chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type + payload) & 0xffffffff
        if actual_crc != expected_crc:
            fail("PNG chunk CRC mismatch")
        if chunk_type == b"IHDR":
            if seen_ihdr or chunk_index != 0 or length != 13:
                fail("PNG must contain exactly one leading IHDR")
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type, compression, filtering, interlace = payload[8:13]
            supported_depths = {2: {8}, 3: {1, 2, 4, 8}, 6: {8}}
            if (color_type not in supported_depths or
                    bit_depth not in supported_depths[color_type] or
                    compression != 0 or filtering != 0 or interlace != 0):
                fail("PNG pixel format is unsupported by the bootloaders")
            seen_ihdr = True
        elif not seen_ihdr:
            fail("PNG IHDR is not the first chunk")
        elif chunk_type == b"PLTE":
            if seen_plte or seen_idat or length == 0 or length > 768 or length % 3:
                fail("PNG palette chunk has invalid order or size")
            if color_type == 3 and length // 3 > 2 ** bit_depth:
                fail("PNG palette exceeds the indexed bit depth")
            seen_plte = True
        elif chunk_type == b"IDAT":
            if idat_closed:
                fail("PNG IDAT chunks are not consecutive")
            seen_idat = True
            idat_bytes += length
            idat_parts.append(payload)
        elif chunk_type == b"IEND":
            if seen_iend or length != 0 or not seen_idat or idat_bytes == 0:
                fail("PNG IEND or IDAT structure is invalid")
            seen_iend = True
            offset = chunk_end
            if offset != len(data):
                fail("PNG has trailing data after IEND")
            break
        else:
            if seen_idat:
                idat_closed = True
            if chr(chunk_type[0]).isupper():
                fail("PNG contains an unknown critical chunk")
        offset = chunk_end
        chunk_index += 1
    if not seen_iend or (color_type == 3 and not seen_plte):
        fail("PNG is missing a required palette, IDAT, or final IEND chunk")
    if not (1 <= width <= 8192 and 1 <= height <= 8192):
        fail("boot background dimensions are outside 1 to 8192 pixels")
    channels = {2: 3, 3: 1, 6: 4}[color_type]
    row_bytes = (width * channels * bit_depth + 7) // 8
    row_size = row_bytes + 1
    rows = 0
    pending = bytearray()
    decompressor = zlib.decompressobj()

    def consume_scanlines(block):
        nonlocal rows
        pending.extend(block)
        while len(pending) >= row_size:
            if pending[0] > 4:
                fail("PNG scanline uses an invalid filter")
            del pending[:row_size]
            rows += 1
            if rows > height:
                fail("PNG decompressed data exceeds its dimensions")

    try:
        for compressed in idat_parts:
            remaining = compressed
            while remaining:
                block = decompressor.decompress(remaining, 65536)
                remaining = decompressor.unconsumed_tail
                consume_scanlines(block)
        consume_scanlines(decompressor.flush())
    except zlib.error:
        fail("PNG IDAT data is not a valid zlib stream")
    if (not decompressor.eof or decompressor.unused_data or pending or rows != height):
        fail("PNG decompressed scanlines do not match its dimensions")
    result = {
        "width": width,
        "height": height,
        "size": metadata.st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return result


def validate_png(path, output_path):
    result = png_metadata(path)
    with open(output_path, "x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    os.chmod(output_path, 0o600)
    for value in (str(result["width"]), str(result["height"]), str(result["size"]),
                  result["sha256"]):
        print(value)


def line_body(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def replace_or_prepend(lines, expression, replacement):
    found = False
    output = []
    for line in lines:
        body, ending = line_body(line)
        if expression.match(body):
            indent = body[:len(body) - len(body.lstrip())]
            output.append(indent + replacement + ending)
            found = True
        else:
            output.append(line)
    if not found:
        output.insert(0, replacement + "\n")
    return output


def boot_semantic(arguments):
    tokens = arguments.split()
    modes = []
    if "perchdir=resume" in tokens:
        modes.append("resume")
    if "perchdir=new" in tokens:
        modes.append("new")
    if "perchdir=ask" in tokens:
        modes.append("choose")
    if "toram" in tokens:
        modes.append("toram")
    if len(modes) > 1:
        fail("boot entry has conflicting MiniOS session arguments")
    if modes:
        return modes[0]
    if "boot=live" in tokens:
        return "fresh"
    return None


GRUB_CLASS_MODES = {
    "resume": "resume", "new": "new", "switch": "choose",
    "live": "fresh", "ram": "toram",
}


def grub_menu_entries(lines):
    entries = []
    index = 0
    while index < len(lines):
        body, _ending = line_body(lines[index])
        if not re.match(r"^\s*menuentry(?:\s|$)", body):
            index += 1
            continue
        balance = body.count("{") - body.count("}")
        if balance <= 0:
            fail("unsupported GRUB menuentry syntax")
        end = index
        while balance > 0:
            end += 1
            if end >= len(lines):
                fail("unterminated GRUB menuentry")
            block_body, _block_ending = line_body(lines[end])
            balance += block_body.count("{") - block_body.count("}")
        entries.append((index, end, body))
        index = end + 1
    return entries


def transform_grub(data, timeout, default_boot, kernel_args):
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeError:
        fail("effective GRUB configuration is not valid UTF-8")
    lines = text.splitlines(True)
    if text and not lines:
        lines = [text]
    if default_boot and any(re.match(r"^\s*submenu(?:\s|$)", line_body(line)[0])
                            for line in lines):
        fail("GRUB submenu defaults require an unsupported compound selector")
    entries = grub_menu_entries(lines)
    semantic_entries = []
    kernel_indexes = []
    for menu_index, (start, end, declaration) in enumerate(entries):
        classes = re.findall(r"--class(?:=|\s+)([A-Za-z0-9_-]+)", declaration)
        known = {GRUB_CLASS_MODES[value] for value in classes if value in GRUB_CLASS_MODES}
        if len(known) > 1:
            fail("GRUB menuentry has conflicting semantic classes")
        argument_modes = []
        entry_kernel_indexes = []
        for line_index in range(start + 1, end):
            body, _ending = line_body(lines[line_index])
            match = re.match(r"^\s*(?:linux|linuxefi|linux16)\s+\S+(?:\s+(.*))?$", body)
            if match:
                if "#" in (match.group(1) or "") or body.rstrip().endswith("\\"):
                    fail("GRUB kernel command uses unsupported comment or continuation syntax")
                entry_kernel_indexes.append(line_index)
                mode = boot_semantic(match.group(1) or "")
                if mode is not None:
                    argument_modes.append(mode)
        if len(set(argument_modes)) > 1:
            fail("GRUB menuentry kernel lines disagree on session semantics")
        inferred = argument_modes[0] if argument_modes else None
        declared = next(iter(known)) if known else None
        if declared is not None and inferred is not None and declared != inferred:
            fail("GRUB semantic class conflicts with kernel arguments")
        semantic = declared or inferred
        if semantic is not None:
            if not entry_kernel_indexes:
                fail("GRUB semantic entry has no kernel command")
            semantic_entries.append((menu_index, semantic))
        kernel_indexes.extend(entry_kernel_indexes)
    references = []
    for line in lines:
        body, _ending = line_body(line)
        match = re.match(r"^\s*(?:configfile|source)\s+(\S+)\s*$", body)
        if match:
            references.append(match.group(1))
        elif re.match(r"^\s*(?:configfile|source)(?:\s|$)", body):
            fail("GRUB config reference uses unsupported syntax")
    session = bool(semantic_entries)
    if kernel_args and session:
        encoded_args = kernel_args
        for line_index in kernel_indexes:
            body, ending = line_body(lines[line_index])
            lines[line_index] = body + " " + encoded_args + ending
    if default_boot and session:
        matches = [menu_index for menu_index, semantic in semantic_entries
                   if semantic == default_boot]
        if len(matches) != 1:
            fail("effective GRUB session menu cannot prove the requested default entry")
        lines = replace_or_prepend(lines, re.compile(r"^\s*set\s+default\s*="),
                                   "set default={}".format(matches[0]))
    if timeout is not None:
        lines = replace_or_prepend(lines, re.compile(r"^\s*set\s+timeout\s*="),
                                   "set timeout={}".format(timeout))
    if (default_boot or kernel_args) and not session and not references:
        fail("effective GRUB config has neither a session menu nor a provable configfile chain")
    return "".join(lines).encode("utf-8"), references, session, len(kernel_indexes)


SYSLINUX_LABEL_MODES = {
    "default": "resume", "perch": "new", "asksession": "choose",
    "live": "fresh", "toram": "toram",
}


def transform_syslinux(data, timeout, default_boot, kernel_args):
    text = data.decode("utf-8", "surrogateescape")
    lines = text.splitlines(True)
    if default_boot and any(re.match(r"^\s*MENU\s+BEGIN(?:\s|$)", line_body(line)[0],
                                     re.IGNORECASE) for line in lines):
        fail("SYSLINUX nested menus require an unsupported default selector")
    label_starts = []
    references = []
    for index, line in enumerate(lines):
        body, _ending = line_body(line)
        match = re.match(r"^\s*LABEL\s+(\S+)\s*$", body, re.IGNORECASE)
        if match:
            label_starts.append((index, match.group(1)))
        config_match = re.match(r"^\s*(?:CONFIG|INCLUDE)\s+(\S+)\s*$", body, re.IGNORECASE)
        if config_match:
            references.append(config_match.group(1))
        elif re.match(r"^\s*(?:CONFIG|INCLUDE)(?:\s|$)", body, re.IGNORECASE):
            fail("SYSLINUX config reference uses unsupported syntax")
    semantic_entries = []
    append_indexes = []
    for position, (start, label) in enumerate(label_starts):
        end = label_starts[position + 1][0] if position + 1 < len(label_starts) else len(lines)
        kernel = False
        appends = []
        for line_index in range(start + 1, end):
            body, _ending = line_body(lines[line_index])
            kernel_match = re.match(r"^\s*(?:KERNEL|LINUX)\s+(\S+)\s*$", body, re.IGNORECASE)
            if kernel_match and "vmlinuz" in kernel_match.group(1).lower():
                kernel = True
            append_match = re.match(r"^\s*APPEND(?:\s+(.*))?$", body, re.IGNORECASE)
            if append_match:
                if "#" in (append_match.group(1) or "") or body.rstrip().endswith("\\"):
                    fail("SYSLINUX APPEND uses unsupported comment or continuation syntax")
                appends.append((line_index, append_match.group(1) or ""))
        if not kernel:
            continue
        if len(appends) != 1:
            fail("SYSLINUX kernel entry must contain exactly one APPEND directive")
        semantic = boot_semantic(appends[0][1])
        label_semantic = SYSLINUX_LABEL_MODES.get(label.lower())
        if semantic is None:
            fail("SYSLINUX kernel entry has no provable MiniOS session semantics")
        if label_semantic is not None and label_semantic != semantic:
            fail("SYSLINUX LABEL conflicts with APPEND session semantics")
        semantic_entries.append((label, semantic))
        append_indexes.append(appends[0][0])
    session = bool(semantic_entries)
    if kernel_args and session:
        for line_index in append_indexes:
            body, ending = line_body(lines[line_index])
            lines[line_index] = body + " " + kernel_args + ending
    if default_boot and session:
        matches = [label for label, semantic in semantic_entries if semantic == default_boot]
        if len(matches) != 1:
            fail("effective SYSLINUX session menu cannot prove the requested default entry")
        lines = [line for line in lines
                 if not re.match(r"^\s*MENU\s+DEFAULT\s*$", line_body(line)[0], re.IGNORECASE)]
        lines = replace_or_prepend(lines, re.compile(r"^\s*DEFAULT(?:\s|$)", re.IGNORECASE),
                                   "DEFAULT {}".format(matches[0]))
        timeout_default = re.compile(r"^\s*ONTIMEOUT(?:\s|$)", re.IGNORECASE)
        lines = [(body[:len(body) - len(body.lstrip())] + "ONTIMEOUT " + matches[0] + ending)
                 if timeout_default.match(body) else line
                 for line in lines for body, ending in [line_body(line)]]
    if timeout is not None:
        lines = replace_or_prepend(lines, re.compile(r"^\s*TIMEOUT(?:\s|$)", re.IGNORECASE),
                                   "TIMEOUT {}".format(timeout * 10))
    if (default_boot or kernel_args) and not session and not references:
        fail("effective SYSLINUX config has neither a session menu nor a provable CONFIG chain")
    return "".join(lines).encode("utf-8", "surrogateescape"), references, session, len(append_indexes)


def validate_syslinux_grub_chainloader(path):
    _metadata, data = read_stable_regular(path, 4 * 1024 * 1024)
    text = data.decode("utf-8", "surrogateescape")
    normalized = [line.strip() for line in text.splitlines() if line.strip()]
    expected = [
        "PROMPT 0",
        "TIMEOUT 1",
        "DEFAULT grub2",
        "LABEL grub2",
        "MENU LABEL GRUB2",
        "LINUX /minios/boot/grub/i386-pc/lnxboot.img",
        "INITRD /minios/boot/grub/i386-pc/core.img",
    ]
    if normalized != expected:
        fail("SYSLINUX-GRUB configuration is not the exact MiniOS chainloader stanza")


def resolve_boot_reference(current, reference, kind):
    if any(character in reference for character in "\"'`$;|&<>(){}[]"):
        fail("boot config reference uses unsupported syntax")
    if kind == "grub" and reference.startswith("/"):
        resolved = posixpath.normpath(reference[1:])
    elif reference.startswith("/"):
        resolved = posixpath.normpath("minios/boot/syslinux/" + reference.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(current), reference))
    if resolved.startswith("../") or resolved == ".." or not resolved.startswith("minios/boot/"):
        fail("boot config reference escapes the MiniOS boot tree")
    return resolved


def load_boot_mapping(path):
    values = read_nul(path)
    if len(values) % 3:
        fail("boot config input mapping is truncated")
    mapping = {}
    for index in range(0, len(values), 3):
        target = values[index].decode("utf-8", "strict")
        source = os.fsdecode(values[index + 1])
        kind = values[index + 2].decode("ascii", "strict")
        if kind not in ("grub", "syslinux") or target in mapping:
            fail("boot config input mapping is invalid")
        mapping[target] = {"source": source, "kind": kind}
    return mapping


def execute_boot_plan(plan, output_directory):
    if not isinstance(plan.get("mapping"), list):
        fail("boot customization plan mapping is invalid")
    mapping = {}
    for item in plan["mapping"]:
        if (not isinstance(item, dict) or
                set(item) != {"target", "source", "kind", "source_size", "source_sha256"} or
                not isinstance(item["target"], str) or not item["target"] or
                not isinstance(item["source"], str) or not item["source"] or
                item["kind"] not in ("grub", "syslinux") or
                not is_strict_int(item["source_size"]) or
                not is_sha256(item["source_sha256"]) or item["target"] in mapping):
            fail("boot customization plan mapping is invalid")
        mapping[item["target"]] = item
    roots = plan["roots"]
    timeout = plan["timeout"]
    default_boot = plan["default_boot"] or ""
    kernel_args = plan["kernel_args"] or ""
    os.mkdir(output_directory, 0o700)
    records = []
    outputs = []
    visiting = set()
    visited = set()

    def visit(target):
        if target in visited:
            return 0
        if target in visiting:
            fail("effective boot config graph contains a cycle")
        item = mapping.get(target)
        if item is None:
            fail("effective boot config references an unavailable config")
        visiting.add(target)
        source_metadata, source_data = read_stable_regular(item["source"], 4 * 1024 * 1024)
        source_digest = hashlib.sha256(source_data).hexdigest()
        if len(source_data) != item["source_size"] or source_digest != item["source_sha256"]:
            fail("boot customization plan source differs from immutable intent")
        if item["kind"] == "grub":
            transformed, references, session, kernel_lines = transform_grub(
                source_data, timeout, default_boot, kernel_args)
        else:
            transformed, references, session, kernel_lines = transform_syslinux(
                source_data, timeout, default_boot, kernel_args)
        output = os.path.join(output_directory, "config-{:08d}".format(len(records)))
        with open(output, "xb") as stream:
            stream.write(transformed)
        os.chmod(output, stat.S_IMODE(source_metadata.st_mode))
        os.utime(output, ns=(source_metadata.st_mtime_ns, source_metadata.st_mtime_ns),
                 follow_symlinks=False)
        digest = hashlib.sha256(transformed).hexdigest()
        records.append({
            "target": target,
            "kind": item["kind"],
            "source": item["source"],
            "source_sha256": source_digest,
            "size": len(transformed),
            "sha256": digest,
            "session": bool(session),
            "kernel_lines": kernel_lines,
        })
        outputs.append((target, output))
        session_count = 1 if session else 0
        for reference in references:
            referenced = resolve_boot_reference(target, reference, item["kind"])
            session_count += visit(referenced)
        visiting.remove(target)
        visited.add(target)
        return session_count

    for root in roots:
        sessions = visit(root)
        if (default_boot or kernel_args) and sessions == 0:
            fail("effective bootloader root has no provable MiniOS session menu")
    return records, outputs


def customize_boot(mapping_path, roots_path, output_directory, plan_path,
                   records_path, output_mapping_path, timeout_text, default_boot,
                   kernel_args):
    mapping = load_boot_mapping(mapping_path)
    roots = [value.decode("utf-8", "strict") for value in read_nul(roots_path)]
    if not roots or any(root not in mapping for root in roots):
        fail("effective boot config roots are invalid")
    timeout = int(timeout_text) if timeout_text else None
    if kernel_args:
        validate_kernel_arguments(kernel_args)
    plan_mapping = []
    for target, item in sorted(mapping.items()):
        _metadata, source_data = read_stable_regular(item["source"], 4 * 1024 * 1024)
        plan_mapping.append({
            "target": target,
            "source": item["source"],
            "kind": item["kind"],
            "source_size": len(source_data),
            "source_sha256": hashlib.sha256(source_data).hexdigest(),
        })
    plan = {
        "mapping": plan_mapping,
        "roots": roots,
        "timeout": timeout,
        "default_boot": default_boot or None,
        "kernel_args": kernel_args or None,
    }
    records, outputs = execute_boot_plan(plan, output_directory)
    with open(plan_path, "x", encoding="utf-8") as stream:
        json.dump(plan, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    with open(records_path, "x", encoding="utf-8") as stream:
        json.dump(records, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    with open(output_mapping_path, "xb") as stream:
        for target, output in outputs:
            stream.write(target.encode("utf-8") + b"\0" + os.fsencode(output) + b"\0")
    for path in (plan_path, records_path, output_mapping_path):
        os.chmod(path, 0o600)


def validate_overlay_component(name):
    try:
        text = name.decode("utf-8", "strict")
    except UnicodeError:
        fail("overlay path is not valid UTF-8")
    if not text or text in (".", "..") or any(not character.isprintable() for character in text):
        fail("overlay path contains an unsafe component")


def overlay_fingerprint(records):
    digest = hashlib.sha256()
    digest.update(b"minios-image-overlay-v1\0")
    for record in sorted(records, key=lambda item: item["path"]):
        for value in (record["path"], record["kind"], str(record["mode"]),
                      str(record.get("mtime_ns", 0)), str(record.get("size", 0)),
                      record.get("digest", ""), record.get("target", "")):
            encoded = os.fsencode(value) if isinstance(value, str) else value
            digest.update(encoded)
            digest.update(b"\0")
    return digest.hexdigest()


def scan_overlay_tree(root_fd, destination=None):
    root_metadata = os.fstat(root_fd)
    records = [{"path": ".", "kind": "directory",
                "mode": stat.S_IMODE(root_metadata.st_mode) & 0o777,
                "mtime_ns": root_metadata.st_mtime_ns}]

    def walk(directory_fd, relative):
        names = sorted((os.fsencode(value) for value in os.listdir(directory_fd)))
        for name in names:
            validate_overlay_component(name)
            path = name if not relative else relative + b"/" + name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            text_path = path.decode("utf-8", "strict")
            output = None if destination is None else os.path.join(os.fsencode(destination), path)
            if stat.S_ISDIR(metadata.st_mode):
                if metadata.st_dev != root_metadata.st_dev:
                    fail("overlay directory crosses a filesystem boundary")
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        fail("overlay directory identity changed")
                    if output is not None:
                        os.mkdir(output, 0o700)
                    records.append({"path": text_path, "kind": "directory",
                                    "mode": stat.S_IMODE(metadata.st_mode) & 0o777,
                                    "mtime_ns": metadata.st_mtime_ns})
                    walk(child_fd, path)
                    if output is not None:
                        os.chmod(output, stat.S_IMODE(metadata.st_mode) & 0o777)
                        os.utime(output, ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                                 follow_symlinks=False)
                    if stable_snapshot(os.fstat(child_fd)) != stable_snapshot(opened):
                        fail("overlay directory changed during snapshot")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    opened = os.fstat(descriptor)
                    if (not stat.S_ISREG(opened.st_mode) or
                            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)):
                        fail("overlay regular file identity changed")
                    digest = hashlib.sha256()
                    output_fd = -1
                    if output is not None:
                        output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                            stat.S_IMODE(opened.st_mode) & 0o777)
                    try:
                        while True:
                            block = os.read(descriptor, 1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                            if output_fd >= 0:
                                offset = 0
                                while offset < len(block):
                                    offset += os.write(output_fd, block[offset:])
                    finally:
                        if output_fd >= 0:
                            os.fchmod(output_fd, stat.S_IMODE(opened.st_mode) & 0o777)
                            os.fsync(output_fd)
                            os.close(output_fd)
                    if stable_snapshot(os.fstat(descriptor)) != stable_snapshot(opened):
                        fail("overlay regular file changed during snapshot")
                    records.append({"path": text_path, "kind": "regular",
                                    "mode": stat.S_IMODE(opened.st_mode) & 0o777,
                                    "mtime_ns": opened.st_mtime_ns,
                                    "size": opened.st_size, "digest": digest.hexdigest()})
                    if output is not None:
                        os.utime(output, ns=(opened.st_atime_ns, opened.st_mtime_ns),
                                 follow_symlinks=False)
                finally:
                    os.close(descriptor)
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.fsencode(os.readlink(name, dir_fd=directory_fd))
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stable_snapshot(after) != stable_snapshot(metadata):
                    fail("overlay symbolic link changed during snapshot")
                if (not target or target.startswith(b"/") or b"\0" in target or
                        any(byte < 32 or byte == 127 for byte in target)):
                    fail("overlay symbolic link target is unsafe")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if resolved == b".." or resolved.startswith(b"../"):
                    fail("overlay symbolic link escapes the overlay root")
                if output is not None:
                    os.symlink(target, output)
                    try:
                        os.utime(output, ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                                 follow_symlinks=False)
                    except (NotImplementedError, OSError):
                        pass
                records.append({"path": text_path, "kind": "symlink", "mode": 0o777,
                                "mtime_ns": metadata.st_mtime_ns,
                                "target": os.fsdecode(target),
                                "digest": hashlib.sha256(target).hexdigest()})
            else:
                fail("overlay tree contains an unsupported filesystem object")

    walk(root_fd, b"")
    return records


def snapshot_overlay(source, destination, metadata_path, pause_path):
    encoded = os.fsencode(source)
    if not encoded.startswith(b"/") or os.path.normpath(encoded) != encoded:
        fail("overlay source path is not normalized and absolute")
    root_fd = open_absolute_directory(encoded)
    try:
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            fail("overlay source is not a real directory")
        os.mkdir(destination, 0o700)
        first = scan_overlay_tree(root_fd, destination)
        if pause_path:
            ready = pause_path + ".ready"
            release = pause_path + ".continue"
            descriptor = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600)
            os.close(descriptor)
            for _ in range(400):
                try:
                    marker = os.stat(release, follow_symlinks=False)
                    if not stat.S_ISREG(marker.st_mode):
                        fail("overlay test release marker is unsafe")
                    break
                except FileNotFoundError:
                    time.sleep(0.05)
            else:
                fail("timed out waiting for overlay test mutation")
        second = scan_overlay_tree(root_fd)
        if first != second:
            fail("overlay input tree changed during private snapshot")
        os.chmod(destination, stat.S_IMODE(root_metadata.st_mode) & 0o777)
        os.utime(destination, ns=(root_metadata.st_atime_ns, root_metadata.st_mtime_ns),
                 follow_symlinks=False)
        current_fd = open_absolute_directory(encoded)
        try:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) != (root_metadata.st_dev, root_metadata.st_ino):
                fail("overlay source directory identity changed")
        finally:
            os.close(current_fd)
        result = {
            "input_tree_fingerprint": overlay_fingerprint(first),
            "entry_count": len(first),
            "regular_bytes": sum(item.get("size", 0) for item in first),
            "entries": first,
        }
        with open(metadata_path, "x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.chmod(metadata_path, 0o600)
        for value in (result["input_tree_fingerprint"], str(result["entry_count"]),
                      str(result["regular_bytes"])):
            print(value)
    finally:
        os.close(root_fd)


def overlay_content_record(record):
    value = {"path": record["path"], "kind": record["kind"], "mode": record["mode"]}
    if record["kind"] == "regular":
        value.update({"size": record["size"], "digest": record["digest"]})
    elif record["kind"] == "symlink":
        value.update({"target": record["target"], "digest": record["digest"]})
    return value


def verify_overlay_tree(metadata_path, extracted_tree):
    metadata = load_json(metadata_path)
    if (not isinstance(metadata, dict) or
            set(metadata) != {"input_tree_fingerprint", "entry_count", "regular_bytes", "entries"} or
            not isinstance(metadata["entries"], list)):
        fail("overlay input manifest has an invalid schema")
    expected_records = metadata["entries"]
    if (metadata["entry_count"] != len(expected_records) or
            metadata["regular_bytes"] != sum(item.get("size", 0) for item in expected_records) or
            metadata["input_tree_fingerprint"] != overlay_fingerprint(expected_records)):
        fail("overlay input manifest summary does not match its entries")
    expected = [overlay_content_record(item) for item in expected_records]
    if len({item["path"] for item in expected}) != len(expected):
        fail("overlay input manifest contains duplicate paths")
    root_fd = open_absolute_directory(extracted_tree)
    try:
        actual = [overlay_content_record(item) for item in scan_overlay_tree(root_fd)]
    finally:
        os.close(root_fd)
    if sorted(actual, key=lambda item: item["path"]) != sorted(expected, key=lambda item: item["path"]):
        fail("SquashFS content does not match the captured overlay manifest")


def generate_customization_report(output_path, boot_records_path, timeout_text,
                                  default_boot, kernel_count, kernel_digest,
                                  background_metadata_path, background_targets_path,
                                  overlay_metadata_path, overlay_module_path,
                                  overlay_target, overlay_order):
    configs = []
    if boot_records_path:
        records = load_json(boot_records_path)
        configs = [{"target": item["target"], "size": item["size"],
                    "sha256": item["sha256"]} for item in records]
        configs.sort(key=lambda item: item["target"])
    background = None
    if background_metadata_path:
        background = load_json(background_metadata_path)
        background["targets"] = sorted(value.decode("utf-8", "strict")
                                       for value in read_nul(background_targets_path))
    overlay = None
    if overlay_metadata_path:
        overlay_input = load_json(overlay_metadata_path)
        size, digest = hash_file(overlay_module_path)
        overlay = {
            "target": overlay_target,
            "module_order": int(overlay_order),
            "size": size,
            "sha256": digest,
            "input_tree_fingerprint": overlay_input["input_tree_fingerprint"],
            "entry_count": overlay_input["entry_count"],
        }
    report = {
        "product_kind": "minios-image-customization",
        "schema_version": 1,
        "boot": {
            "timeout_seconds": int(timeout_text) if timeout_text else None,
            "default_boot": default_boot or None,
            "kernel_args": ({"bytes": int(kernel_count), "sha256": kernel_digest}
                            if kernel_digest else None),
            "configs": configs,
            "background": background,
        },
        "overlay": overlay,
    }
    with open(output_path, "x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    os.chmod(output_path, 0o600)


def mapping_dictionary(path):
    values = read_nul(path)
    if len(values) % 2:
        fail("verification mapping is truncated")
    mapping = {}
    for index in range(0, len(values), 2):
        target = values[index].decode("utf-8", "strict")
        if target in mapping:
            fail("verification mapping contains a duplicate target")
        mapping[target] = os.fsdecode(values[index + 1])
    return mapping


def is_sha256(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def is_strict_int(value, minimum=0):
    return type(value) is int and value >= minimum


def validate_extraction_footprint(footprint):
    integer_fields = {
        "regular_file_bytes", "regular_file_inodes", "directory_count",
        "symlink_count", "symlink_target_bytes", "whiteout_count",
        "inode_count", "directory_entry_count", "filename_bytes",
        "hardlink_reference_count", "xattr_count", "xattr_name_bytes",
        "xattr_value_bytes", "block_size",
    }
    expected_keys = integer_fields | {
        "product_kind", "schema_version", "compressor",
    }
    if (not isinstance(footprint, dict) or set(footprint) != expected_keys or
            footprint["product_kind"] != "minios-extraction-footprint" or
            type(footprint["schema_version"]) is not int or
            footprint["schema_version"] != 1 or
            any(not is_strict_int(footprint[name]) for name in integer_fields) or
            footprint["compressor"] not in ("zstd", "gzip", "lzo", "xz")):
        fail("capture metadata extraction footprint schema is invalid")
    block_size = footprint["block_size"]
    if (block_size < 4096 or block_size > 1024 * 1024 or
            block_size & (block_size - 1)):
        fail("capture metadata extraction footprint block size is invalid")
    regular_inodes = footprint["regular_file_inodes"]
    directories = footprint["directory_count"]
    symlinks = footprint["symlink_count"]
    whiteouts = footprint["whiteout_count"]
    hardlinks = footprint["hardlink_reference_count"]
    entry_count = footprint["directory_entry_count"]
    if (directories < 1 or
            (hardlinks and not regular_inodes) or
            entry_count != (directories - 1 + regular_inodes + hardlinks +
                            symlinks + whiteouts) or
            footprint["inode_count"] != (directories + regular_inodes +
                                          symlinks + whiteouts) or
            footprint["filename_bytes"] < entry_count or
            footprint["symlink_target_bytes"] < symlinks or
            footprint["xattr_name_bytes"] < footprint["xattr_count"]):
        fail("capture metadata extraction footprint sizing is invalid")


def validate_capture_module_metadata(module, size, digest):
    required_keys = {"size", "sha256"}
    additive_keys = {"entry_count", "uncompressed_size",
                     "extraction_footprint"}
    if (not isinstance(module, dict) or
            not required_keys.issubset(module) or
            not set(module).issubset(required_keys | additive_keys) or
            not is_strict_int(module["size"], 1) or
            not is_sha256(module["sha256"])):
        fail("capture metadata module schema is invalid")
    if "entry_count" in module and not is_strict_int(module["entry_count"]):
        fail("capture metadata module entry count is invalid")
    if ("uncompressed_size" in module and
            not is_strict_int(module["uncompressed_size"])):
        fail("capture metadata module uncompressed size is invalid")
    if "extraction_footprint" in module:
        footprint = module["extraction_footprint"]
        validate_extraction_footprint(footprint)
        if ("entry_count" in module and
                footprint["directory_entry_count"] != module["entry_count"]):
            fail("capture metadata module entry count differs from extraction footprint")
        if ("uncompressed_size" in module and
                footprint["regular_file_bytes"] != module["uncompressed_size"]):
            fail("capture metadata module size differs from extraction footprint")
    if module["size"] != size or module["sha256"] != digest:
        fail("capture metadata does not match captured module")


def validate_customization_attestation_schema(report):
    if (not isinstance(report, dict) or
            set(report) != {"product_kind", "schema_version", "boot", "overlay"} or
            report["product_kind"] != "minios-image-customization" or
            type(report["schema_version"]) is not int or report["schema_version"] != 1):
        fail("image customization attestation identity or top-level schema is invalid")
    boot = report["boot"]
    if (not isinstance(boot, dict) or
            set(boot) != {"timeout_seconds", "default_boot", "kernel_args",
                          "configs", "background"}):
        fail("image customization boot attestation schema is invalid")
    timeout = boot["timeout_seconds"]
    if timeout is not None and (not is_strict_int(timeout) or timeout > 300):
        fail("image customization timeout attestation is invalid")
    if boot["default_boot"] is not None and boot["default_boot"] not in (
            "resume", "new", "choose", "fresh", "toram"):
        fail("image customization default attestation is invalid")
    kernel = boot["kernel_args"]
    if kernel is not None and (not isinstance(kernel, dict) or
            set(kernel) != {"bytes", "sha256"} or
            not is_strict_int(kernel["bytes"], 1) or kernel["bytes"] > 4096 or
            not is_sha256(kernel["sha256"])):
        fail("image customization kernel-argument attestation is invalid")
    configs = boot["configs"]
    if not isinstance(configs, list):
        fail("image customization config attestation is not a list")
    config_targets = []
    for item in configs:
        if (not isinstance(item, dict) or set(item) != {"target", "size", "sha256"} or
                not isinstance(item["target"], str) or not item["target"] or
                not is_strict_int(item["size"]) or not is_sha256(item["sha256"])):
            fail("image customization config attestation entry is invalid")
        config_targets.append(item["target"])
    if len(set(config_targets)) != len(config_targets):
        fail("image customization config attestation contains duplicate targets")
    background = boot["background"]
    if background is not None:
        if (not isinstance(background, dict) or
                set(background) != {"width", "height", "size", "sha256", "targets"} or
                not is_strict_int(background["width"], 1) or background["width"] > 8192 or
                not is_strict_int(background["height"], 1) or background["height"] > 8192 or
                not is_strict_int(background["size"], 1) or
                not is_sha256(background["sha256"]) or
                not isinstance(background["targets"], list) or
                any(not isinstance(target, str) or not target
                    for target in background["targets"]) or
                len(set(background["targets"])) != len(background["targets"])):
            fail("image customization background attestation is invalid")
    overlay = report["overlay"]
    if overlay is not None and (not isinstance(overlay, dict) or
            set(overlay) != {"target", "module_order", "size", "sha256",
                             "input_tree_fingerprint", "entry_count"} or
            not isinstance(overlay["target"], str) or not overlay["target"] or
            not is_strict_int(overlay["module_order"]) or
            not is_strict_int(overlay["size"], 1) or not is_sha256(overlay["sha256"]) or
            not is_sha256(overlay["input_tree_fingerprint"]) or
            not is_strict_int(overlay["entry_count"], 1)):
        fail("image customization overlay attestation is invalid")


def parse_presence(value, name):
    if value not in ("true", "false"):
        fail("invalid {} presence intent".format(name))
    return value == "true"


def verify_target_set(targets, count_text, digest, name):
    count, actual_digest = target_set_identity(targets)
    if str(count) != count_text or actual_digest != digest:
        fail("{} target set differs from immutable build intent".format(name))


def verify_customization(extracted_report_path, boot_plan_path, extracted_boot_mapping,
                         recompute_directory, extracted_background_mapping,
                         extracted_overlay_path, local_overlay_path, boot_presence,
                         timeout_text, default_boot, kernel_args, boot_plan_size,
                         boot_plan_digest, boot_target_count, boot_target_digest,
                         background_presence, background_width, background_height,
                         background_size, background_digest, background_target_count,
                          background_target_digest, overlay_presence, overlay_target,
                          overlay_order, overlay_size, overlay_digest,
                          overlay_fingerprint, overlay_entry_count):
    report = load_json(extracted_report_path)
    validate_customization_attestation_schema(report)
    boot_present = parse_presence(boot_presence, "boot customization")
    background_present = parse_presence(background_presence, "background")
    overlay_present = parse_presence(overlay_presence, "overlay")
    expected_timeout = int(timeout_text) if timeout_text else None
    expected_default = default_boot or None
    expected_kernel = None
    if kernel_args:
        count, digest = validate_kernel_arguments(kernel_args)
        expected_kernel = {"bytes": count, "sha256": digest}

    expected_configs = []
    extracted_configs = mapping_dictionary(extracted_boot_mapping)
    if boot_present:
        plan_size, plan_digest = hash_file(boot_plan_path)
        if str(plan_size) != boot_plan_size or plan_digest != boot_plan_digest:
            fail("boot customization plan differs from immutable build intent")
        plan = load_json(boot_plan_path)
        if (not isinstance(plan, dict) or
                set(plan) != {"mapping", "roots", "timeout", "default_boot", "kernel_args"} or
                plan["timeout"] != expected_timeout or plan["default_boot"] != expected_default or
                plan["kernel_args"] != (kernel_args or None)):
            fail("boot customization plan intent is invalid")
        recomputed, _outputs = execute_boot_plan(plan, recompute_directory)
        recomputed_configs = {item["target"]: (item["size"], item["sha256"])
                              for item in recomputed}
        verify_target_set(list(extracted_configs), boot_target_count,
                          boot_target_digest, "boot config")
        if set(extracted_configs) != set(recomputed_configs):
            fail("extracted effective boot config set is incomplete")
        for target, path in extracted_configs.items():
            size, digest = hash_file(path)
            if (size, digest) != recomputed_configs[target]:
                fail("embedded boot config does not match recomputed transformation")
            expected_configs.append({"target": target, "size": size, "sha256": digest})
        expected_configs.sort(key=lambda item: item["target"])
    elif (boot_plan_path or extracted_configs or timeout_text or default_boot or kernel_args or
          boot_plan_size or boot_plan_digest or boot_target_count != "0" or boot_target_digest):
        fail("absent boot customization has unexpected verification state")

    extracted_backgrounds = mapping_dictionary(extracted_background_mapping)
    expected_background = None
    if background_present:
        verify_target_set(list(extracted_backgrounds), background_target_count,
                          background_target_digest, "background")
        original_background = {
            "width": int(background_width),
            "height": int(background_height),
            "size": int(background_size),
            "sha256": background_digest,
        }
        for path in extracted_backgrounds.values():
            if png_metadata(path) != original_background:
                fail("embedded boot background differs from immutable input intent")
        expected_background = dict(original_background)
        expected_background["targets"] = sorted(extracted_backgrounds)
    elif (extracted_backgrounds or background_width or background_height or background_size or
          background_digest or background_target_count != "0" or background_target_digest):
        fail("absent background customization has unexpected verification state")

    expected_overlay = None
    if overlay_present:
        local_size, local_digest = hash_file(local_overlay_path)
        embedded_size, embedded_digest = hash_file(extracted_overlay_path)
        if str(local_size) != overlay_size or local_digest != overlay_digest:
            fail("generated image overlay differs from immutable build intent")
        if (local_size, local_digest) != (embedded_size, embedded_digest):
            fail("generated and embedded image overlay modules differ")
        expected_overlay = {
            "target": overlay_target,
            "module_order": int(overlay_order),
            "size": int(overlay_size),
            "sha256": overlay_digest,
            "input_tree_fingerprint": overlay_fingerprint,
            "entry_count": int(overlay_entry_count),
        }
    elif any((extracted_overlay_path, local_overlay_path, overlay_target, overlay_order,
              overlay_size, overlay_digest, overlay_fingerprint, overlay_entry_count)):
        fail("absent overlay customization has unexpected verification state")

    expected_report = {
        "product_kind": "minios-image-customization",
        "schema_version": 1,
        "boot": {
            "timeout_seconds": expected_timeout,
            "default_boot": expected_default,
            "kernel_args": expected_kernel,
            "configs": expected_configs,
            "background": expected_background,
        },
        "overlay": expected_overlay,
    }
    if report != expected_report:
        fail("embedded image customization attestation differs from verified effective state")


def generate_report(metadata_path, module_path, target, selection_digest, order,
                    expected_profile, output_path, expected_base_fingerprint,
                    require_binding):
    metadata = load_json(metadata_path)
    expected_keys = {"product_kind", "schema_version", "profile", "union_backend",
                     "source_fingerprint", "boot_id", "base_module_fingerprint",
                     "module", "selection_sha256"}
    if not isinstance(metadata, dict) or set(metadata) != expected_keys:
        fail("capture metadata has an invalid schema")
    if (metadata["product_kind"] != "minios-session-capture-metadata" or
            type(metadata["schema_version"]) is not int or
            metadata["schema_version"] != 2):
        fail("capture metadata identity is invalid")
    if metadata["profile"] != expected_profile:
        fail("capture metadata profile mismatch")
    if metadata["union_backend"] not in ("overlayfs", "aufs", "unknown"):
        fail("capture metadata union backend is invalid")
    if (not isinstance(metadata["source_fingerprint"], str) or
            len(metadata["source_fingerprint"]) != 64 or
            any(character not in "0123456789abcdef" for character in metadata["source_fingerprint"])):
        fail("capture metadata source fingerprint is invalid")
    if (not isinstance(metadata["boot_id"], str) or not metadata["boot_id"] or
            len(metadata["boot_id"]) > 128):
        fail("capture metadata boot identity is invalid")
    base_fingerprint = metadata["base_module_fingerprint"]
    if base_fingerprint is not None and (not isinstance(base_fingerprint, str) or
            len(base_fingerprint) != 64 or
            any(character not in "0123456789abcdef" for character in base_fingerprint)):
        fail("capture metadata base module fingerprint is invalid")
    if base_fingerprint is not None and base_fingerprint != expected_base_fingerprint:
        fail("captured running base modules do not match the ISO source")
    if require_binding == "true" and base_fingerprint is None:
        fail("explicit ISO source cannot be bound to the running capture source")
    size, digest = hash_file(module_path)
    validate_capture_module_metadata(metadata["module"], size, digest)
    expected_selection = selection_digest or None
    if metadata["selection_sha256"] != expected_selection:
        fail("capture metadata selection digest mismatch")
    report = {
        "product_kind": "minios-session-capture-report",
        "schema_version": 3,
        "profile": metadata["profile"],
        "union_backend": metadata["union_backend"],
        "source_fingerprint": metadata["source_fingerprint"],
        "boot_id": metadata["boot_id"],
        "base_module_fingerprint": base_fingerprint,
        "module_order": int(order),
        "module": {"target": target, "size": size, "sha256": digest},
        "selection_sha256": expected_selection,
    }
    with open(output_path, "x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    os.chmod(output_path, 0o600)
    for value in (str(size), digest, metadata["union_backend"], metadata["source_fingerprint"],
                  metadata["boot_id"], base_fingerprint or ""):
        print(value)


def validate_extracted(report_path, module_path, expected_target, expected_selection,
                       metadata_path, manifest_path, manifest_digest, expected_order):
    report = load_json(report_path)
    metadata = load_json(metadata_path)
    expected_report_keys = {"product_kind", "schema_version", "profile", "union_backend",
                            "source_fingerprint", "boot_id", "base_module_fingerprint",
                            "module_order", "module", "selection_sha256"}
    if not isinstance(report, dict) or set(report) != expected_report_keys:
        fail("embedded capture report schema is invalid")
    if report["product_kind"] != "minios-session-capture-report" or report["schema_version"] != 3:
        fail("embedded capture report identity is invalid")
    size, digest = hash_file(module_path)
    if report["module"] != {"target": expected_target, "size": size, "sha256": digest}:
        fail("embedded capture module bytes do not match report")
    validate_capture_module_metadata(metadata.get("module"), size, digest)
    if report["selection_sha256"] != (expected_selection or None):
        fail("embedded selection digest mismatch")
    if report["module_order"] != int(expected_order):
        fail("embedded capture module order mismatch")
    if (report["profile"] != metadata["profile"] or
            report["union_backend"] != metadata["union_backend"] or
            report["source_fingerprint"] != metadata["source_fingerprint"] or
            report["boot_id"] != metadata["boot_id"] or
            report["base_module_fingerprint"] != metadata["base_module_fingerprint"]):
        fail("embedded report does not match privileged capture metadata")
    if manifest_path:
        _size, digest_value = hash_file(manifest_path)
        if digest_value != manifest_digest:
            fail("embedded build manifest digest mismatch")


def verify_requested_parent(path, expected_dev, expected_ino):
    descriptor = open_absolute_directory(os.path.realpath(os.path.abspath(os.fsencode(path))))
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (int(expected_dev), int(expected_ino)):
            fail("requested output parent identity changed")
    finally:
        os.close(descriptor)


def publish_iso(arguments):
    (parent_fd_text, work_fd_text, parent_path, parent_dev, parent_ino, work_name,
     work_dev, work_ino, target, target_state, overwrite, expected_hash) = arguments
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    parent_fd = os.dup(int(parent_fd_text))
    work_fd = os.dup(int(work_fd_text))
    target_bytes = os.fsencode(target)
    try:
        check_output_fds(parent_fd_text, work_fd_text, parent_dev, parent_ino,
                         work_name, work_dev, work_ino)
        verify_requested_parent(parent_path, parent_dev, parent_ino)
        image = os.stat(b"image.iso", dir_fd=work_fd, follow_symlinks=False)
        image_fd = os.open(b"image.iso", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=work_fd)
        try:
            if (not stat.S_ISREG(image.st_mode) or image.st_size <= 0 or
                    hash_fd(image_fd, image) != expected_hash):
                fail("private ISO identity or digest changed before publication")
        finally:
            os.close(image_fd)
        try:
            current = os.stat(target_bytes, dir_fd=parent_fd, follow_symlinks=False)
            current_state = "present:{}:{}:{}".format(
                current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        except FileNotFoundError:
            current_state = "absent"
        if current_state != target_state:
            fail("output target identity changed before publication")
        if target_state == "absent":
            method = rename_noreplace(work_fd, b"image.iso", parent_fd, target_bytes)
            if method == "link":
                os.unlink(b"image.iso", dir_fd=work_fd)
        else:
            if overwrite != "true":
                fail("overwrite was not authorized")
            os.replace(b"image.iso", target_bytes, src_dir_fd=work_fd, dst_dir_fd=parent_fd)
        published = os.stat(target_bytes, dir_fd=parent_fd, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (image.st_dev, image.st_ino):
            fail("published ISO identity mismatch")
        try:
            os.fsync(parent_fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
    finally:
        os.close(work_fd)
        os.close(parent_fd)


def cleanup_output(parent_fd_text, work_fd_text, parent_dev, parent_ino,
                   work_name, work_dev, work_ino):
    parent_fd = os.dup(int(parent_fd_text))
    work_fd = os.dup(int(work_fd_text))
    try:
        parent = os.fstat(parent_fd)
        work = os.fstat(work_fd)
        if (parent.st_dev, parent.st_ino) != (int(parent_dev), int(parent_ino)):
            return
        if (work.st_dev, work.st_ino) != (int(work_dev), int(work_ino)):
            return
        clear_directory(work_fd)
        candidates = [os.fsdecode(work_name)] + os.listdir(parent_fd)
        seen = set()
        for name in candidates:
            if name in seen:
                continue
            seen.add(name)
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (named.st_dev, named.st_ino) == (work.st_dev, work.st_ino):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
                break
    finally:
        os.close(work_fd)
        os.close(parent_fd)


def clear_directory(directory_fd):
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory_fd)
            try:
                clear_directory(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def main(arguments):
    command = arguments[0]
    values = arguments[1:]
    if command == "validate-json" and len(values) == 1:
        validate_json_object(values[0])
    elif command == "snapshot-file" and len(values) == 2:
        snapshot_file(*values)
    elif command == "prepare-output" and len(values) == 3:
        prepare_output(*values)
    elif command == "check-fds" and len(values) == 7:
        check_output_fds(*values)
    elif command == "record-inputs" and len(values) == 2:
        record_inputs(*values)
    elif command == "verify-inputs" and len(values) == 2:
        verify_inputs(*values)
    elif command == "module-fingerprint" and len(values) == 2:
        module_fingerprint_from_records(*values)
    elif command == "snapshot-inputs" and len(values) == 4:
        snapshot_inputs(*values)
    elif command == "snapshot-space" and len(values) == 2:
        snapshot_fallback_size(*values)
    elif command == "target-set" and len(values) == 2:
        target_set_file(*values)
    elif command == "validate-kernel-args" and len(values) == 1:
        count, digest = validate_kernel_arguments(values[0])
        print(count)
        print(digest)
    elif command == "validate-png" and len(values) == 2:
        validate_png(*values)
    elif command == "customize-boot" and len(values) == 9:
        customize_boot(*values)
    elif command == "validate-syslinux-grub" and len(values) == 1:
        validate_syslinux_grub_chainloader(values[0])
    elif command == "snapshot-overlay" and len(values) == 4:
        snapshot_overlay(*values)
    elif command == "verify-overlay-tree" and len(values) == 2:
        verify_overlay_tree(*values)
    elif command == "generate-customization-report" and len(values) == 12:
        generate_customization_report(*values)
    elif command == "verify-customization" and len(values) == 29:
        verify_customization(*values)
    elif command == "hash-file" and len(values) == 1:
        size, digest = hash_file(values[0])
        print(size)
        print(digest)
    elif command == "generate-report" and len(values) == 9:
        generate_report(*values)
    elif command == "validate-extracted" and len(values) == 8:
        validate_extracted(*values)
    elif command == "publish" and len(values) == 12:
        publish_iso(values)
    elif command == "cleanup" and len(values) == 7:
        cleanup_output(*values)
    else:
        fail("invalid isolated adapter helper invocation")


try:
    main(sys.argv[1:])
except AdapterError as error:
    print("E: {}".format(error), file=sys.stderr)
    raise SystemExit(1)
except (OSError, TypeError, UnicodeError, ValueError) as error:
    print("E: isolated adapter helper failed: {}".format(error), file=sys.stderr)
    raise SystemExit(1)
