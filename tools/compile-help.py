#!/usr/bin/env python3
"""Compile Image Builder Markdown help into parser-free MiniOS documents."""

from __future__ import absolute_import, print_function

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PRODUCT_KIND = "minios-image-builder-help"
SCHEMA_VERSION = 1


class CompileHelpError(Exception):
    pass


def default_paths():
    root = Path(__file__).resolve().parent.parent
    compiler = root.parent / "minios-gui" / "tools" / "markdown-compiler.mjs"
    return root / "help", root / "share" / "help", compiler


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def collect_sources(source_root):
    sources = []
    for path in sorted(source_root.rglob("*.md")):
        if path.is_symlink():
            raise CompileHelpError("help source must not be a symlink: {}".format(path))
        relative = path.relative_to(source_root)
        if len(relative.parts) < 2:
            raise CompileHelpError("help source must be below a locale directory: {}".format(relative))
        output = relative.with_suffix(".json")
        sources.append((path, relative, output))
    if not sources:
        raise CompileHelpError("no Markdown help sources found")
    return sources


def compile_help(source_root, output_root, compiler, node="node", mermaid_command=None):
    source_root = Path(source_root).resolve()
    output_root = Path(output_root)
    compiler = Path(compiler).resolve()
    if not source_root.is_dir():
        raise CompileHelpError("help source directory does not exist: {}".format(source_root))
    if not compiler.is_file():
        raise CompileHelpError("shared Markdown compiler is missing: {}".format(compiler))

    sources = collect_sources(source_root)
    parent = output_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=".image-builder-help-", dir=str(parent)))
    batch_path = temp_root / ".compile-batch.json"
    try:
        items = []
        for index, (path, _relative, output) in enumerate(sources):
            items.append({
                "id": str(index),
                "text": path.read_text(encoding="utf-8"),
                "source_path": str(path),
                "output_path": output.as_posix(),
            })
        request = {
            "schema_version": 1,
            "docs_root": str(source_root),
            "output_root": str(temp_root),
            "mermaid_command": mermaid_command,
            "items": items,
        }
        batch_path.write_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        try:
            result = subprocess.run(
                [node, str(compiler), str(batch_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CompileHelpError("shared Markdown compiler failed: {}".format(error))
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown error").strip()
            raise CompileHelpError("shared Markdown compiler failed: {}".format(message))
        try:
            response = json.loads(result.stdout)
        except ValueError as error:
            raise CompileHelpError("shared Markdown compiler returned invalid JSON: {}".format(error))
        if response.get("schema_version") != 1:
            raise CompileHelpError("unsupported shared compiler response")
        batch_path.unlink()

        documents = []
        response_items = {item["id"]: item for item in response.get("items", [])}
        for index, (_path, relative, output) in enumerate(sources):
            item = response_items.get(str(index))
            generated = temp_root / output
            if item is None or not generated.is_file():
                raise CompileHelpError("compiler did not produce {}".format(output))
            digest = sha256_file(generated)
            if digest != item.get("sha256"):
                raise CompileHelpError("compiler checksum mismatch for {}".format(output))
            documents.append({
                "source": relative.as_posix(),
                "compiled": output.as_posix(),
                "sha256": digest,
            })
        locales = sorted(set(item[1].parts[0] for item in sources))
        assets = response.get("assets", {})
        manifest = {
            "product_kind": PRODUCT_KIND,
            "schema_version": SCHEMA_VERSION,
            "locales": locales,
            "documents": documents,
            "assets": dict(sorted(assets.items())),
        }
        (temp_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")

        backup = None
        if output_root.exists():
            backup = Path(tempfile.mkdtemp(prefix=".image-builder-help-old-", dir=str(parent)))
            backup.rmdir()
            os.rename(str(output_root), str(backup))
        try:
            os.rename(str(temp_root), str(output_root))
        except Exception:
            if backup is not None and backup.exists() and not output_root.exists():
                os.rename(str(backup), str(output_root))
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(str(backup))
        return manifest
    except Exception:
        if temp_root.exists():
            shutil.rmtree(str(temp_root), ignore_errors=True)
        raise


def main(argv=None):
    source, output, compiler = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(source))
    parser.add_argument("--output-root", default=str(output))
    parser.add_argument("--compiler", default=os.environ.get(
        "MINIOS_MARKDOWN_COMPILER", str(compiler)))
    parser.add_argument("--node", default=os.environ.get("MINIOS_NODE", "node"))
    parser.add_argument("--mermaid-command", default=os.environ.get("MINIOS_MERMAID_COMMAND"))
    args = parser.parse_args(argv)
    try:
        manifest = compile_help(
            args.source_root, args.output_root, args.compiler,
            node=args.node, mermaid_command=args.mermaid_command)
    except (CompileHelpError, OSError, UnicodeDecodeError) as error:
        print("compile-help.py: error: {}".format(error), file=sys.stderr)
        return 1
    print("Compiled {} help documents in {} locales".format(
        len(manifest["documents"]), len(manifest["locales"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
