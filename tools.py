import os
import re
import subprocess
from crewai.tools import tool


@tool("Write File Tool")
def write_file_tool(filename: str, content: str) -> str:
    """Writes content to a file in the project workspace.
    
    Args:
        filename: Relative path to the file (e.g. 'main.tf', 'src/app.py').
        content:  The full text content to write into the file.
    """
    try:
        base_dir = os.environ.get("PROJECT_WORKSPACE_DIR", ".")
        filepath = os.path.abspath(os.path.join(base_dir, filename))
        
        # Only create parent directories if there actually are any.
        parent = os.path.dirname(filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        
        with open(filepath, 'w') as f:
            f.write(content)
        return f"Success: Wrote {len(content)} characters to {filepath}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


@tool("Read File Tool")
def read_file_tool(filename: str) -> str:
    """Reads the content of a file in the project workspace.
    
    Args:
        filename: Relative path to the file to read.
    """
    try:
        base_dir = os.environ.get("PROJECT_WORKSPACE_DIR", ".")
        filepath = os.path.abspath(os.path.join(base_dir, filename))
        with open(filepath, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"


@tool("List Directory Tool")
def list_directory_tool(directory: str = ".") -> str:
    """Lists the files and folders in a directory within the project workspace.
    
    Args:
        directory: Relative path to the directory to list. Defaults to the root workspace.
    """
    try:
        base_dir = os.environ.get("PROJECT_WORKSPACE_DIR", ".")
        target_dir = os.path.abspath(os.path.join(base_dir, directory))
        files = os.listdir(target_dir)
        return f"Files in '{target_dir}':\n" + "\n".join(files)
    except Exception as e:
        return f"Error listing directory: {str(e)}"


# ==========================================
# Pure-Python commit step
# ==========================================
# Called directly by app.py after the auditor completes. Replaces the
# git_committer LLM agent — parsing markdown is mechanical and doesn't
# need a model.

_FILE_BLOCK = re.compile(
    r"^###\s*File:\s*(\S+)\s*\n```[a-zA-Z+\-_.]*\n(.*?)(?:\n```|(?=^###\s*File:)|\Z)",
    re.MULTILINE | re.DOTALL,
)
_AUDIT_NOTES = re.compile(
    r"^##\s*Audit Notes\s*\n(.*?)(?=^###\s*File:|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _iter_file_blocks(text: str):
    """Yield (filename, content, closed) for each '### File:' block.

    closed=False means the fenced block never reached its closing ``` — i.e. the
    LLM response was cut off mid-file (typically by hitting max_tokens). Callers
    must not write a truncated block over a good file.
    """
    for m in _FILE_BLOCK.finditer(text):
        closed = m.group(0).rstrip().endswith("```")
        yield m.group(1), m.group(2), closed

# Comment prefix by file extension — used when concatenating same-named files.
_COMMENT_PREFIX: dict[str, str] = {
    "tf": "#", "hcl": "#",
    "py": "#", "yaml": "#", "yml": "#", "sh": "#", "bash": "#",
    "go": "//", "js": "//", "ts": "//", "java": "//", "cs": "//", "cpp": "//",
    "sql": "--",
}


def _sep(filename: str) -> str:
    """Return a comment-style separator appropriate for the file's language."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    prefix = _COMMENT_PREFIX.get(ext, "#")
    return f"\n\n{prefix} ─── additional specialist contribution ───\n\n"


_VAR_REF       = re.compile(r'\bvar\.([a-zA-Z_]\w*)\b')
_BOOL_NAME     = re.compile(r'^(?:enable|is|use|has|allow|create|disable)_|_(?:enabled|flag)$')
_NUMBER_NAME   = re.compile(r'_(?:count|size|port|timeout|ttl|days|gb|min|max|weight|replicas|iops)$')


def _infer_type(name: str) -> str:
    if _BOOL_NAME.search(name):
        return 'bool'
    if _NUMBER_NAME.search(name):
        return 'number'
    return 'string'


def generate_variables(tf_texts: list[str]) -> str | None:
    """Scan Terraform content for var.* references and return variables.tf content.

    Returns None if no variable references are found.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for text in tf_texts:
        for m in _VAR_REF.finditer(text):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                ordered.append(name)

    if not ordered:
        return None

    blocks: list[str] = []
    for name in sorted(ordered):
        var_type    = _infer_type(name)
        description = name.replace('_', ' ').capitalize()
        blocks.append(
            f'variable "{name}" {{\n'
            f'  description = "{description}"\n'
            f'  type        = {var_type}\n'
            f'}}'
        )
    return '\n\n'.join(blocks) + '\n'


# --- outputs.tf generation (pure Python — replaces the Terraform Assembler agent) ---
# Maps an AWS resource type to the attributes worth exposing as outputs. Only
# attributes known to exist on that resource are listed, so the generated
# outputs.tf is always valid HCL. Unknown resource types fall back to `id` only,
# which every Terraform resource exposes.
_RESOURCE_DECL = re.compile(r'resource\s+"([a-zA-Z0-9_]+)"\s+"([a-zA-Z0-9_]+)"')
_OUTPUT_ATTRS: dict[str, list[str]] = {
    'aws_s3_bucket':        ['id', 'arn', 'bucket_domain_name', 'bucket_regional_domain_name'],
    'aws_db_instance':      ['id', 'arn', 'endpoint', 'port'],
    'aws_rds_cluster':      ['id', 'arn', 'endpoint', 'reader_endpoint', 'port'],
    'aws_lambda_function':  ['arn', 'function_name', 'invoke_arn'],
    'aws_vpc':              ['id', 'arn', 'cidr_block'],
    'aws_subnet':           ['id', 'arn', 'cidr_block'],
    'aws_iam_role':         ['id', 'arn', 'name'],
    'aws_iam_policy':       ['id', 'arn'],
    'aws_instance':         ['id', 'arn', 'public_ip', 'private_ip'],
    'aws_security_group':   ['id', 'arn'],
    'aws_kms_key':          ['id', 'arn'],
    'aws_sns_topic':        ['id', 'arn'],
    'aws_sqs_queue':        ['id', 'arn', 'url'],
    'aws_dynamodb_table':   ['id', 'arn'],
    'aws_cloudwatch_log_group': ['arn', 'name'],
}


def generate_outputs(tf_texts: list[str]) -> str | None:
    """Scan Terraform content for resource declarations and return outputs.tf content.

    For each `resource "type" "name"` found, emits outputs for the useful
    attributes of that type (id/arn plus type-specific extras). Unknown types
    get an `id` output only. Returns None if no resources are found.

    This replaces the LLM-backed Terraform Assembler: the transform is purely
    mechanical, so doing it in Python saves a full agent's worth of output tokens.
    """
    # Output names use {type_short}_{rname}_{attr} to avoid collisions when
    # multiple resource types share the same local name (e.g. aws_vpc.main and
    # aws_flow_log.main would both produce "main_id" under a name-only scheme).
    seen: set[tuple[str, str]] = set()
    used_names: set[str] = set()
    blocks: list[str] = []
    for text in tf_texts:
        for rtype, rname in _RESOURCE_DECL.findall(text):
            if (rtype, rname) in seen:
                continue
            seen.add((rtype, rname))
            attrs = _OUTPUT_ATTRS.get(rtype, ['id'])
            type_short = rtype.replace("aws_", "").replace("google_", "").replace("azurerm_", "")
            prefix = f"{type_short}_{rname}"
            for attr in attrs:
                out_name = f"{prefix}_{attr}"
                if out_name in used_names:
                    continue
                used_names.add(out_name)
                blocks.append(
                    f'output "{out_name}" {{\n'
                    f'  description = "{attr} of {rtype}.{rname}"\n'
                    f'  value       = {rtype}.{rname}.{attr}\n'
                    f'}}'
                )
    if not blocks:
        return None
    return '\n\n'.join(blocks) + '\n'


def build_resource_manifest(*texts: str) -> str | None:
    """Compact list of the Terraform resource addresses found in the given text(s).

    Used to tell each specialist which resources already exist — so it references
    them by their exact `<type>.<name>` address and creates any required glue (IAM
    roles, SG rules, etc.) — without pasting full file contents into the prompt.
    Returns None if no resources are found.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for text in texts:
        if not text:
            continue
        for rtype, rname in _RESOURCE_DECL.findall(text):
            addr = f"{rtype}.{rname}"
            if addr not in seen:
                seen.add(addr)
                ordered.append(addr)
    if not ordered:
        return None
    return "\n".join(f"- {a}" for a in ordered)


_FINDING_FILE = re.compile(r"\*\*File:\*\*\s*`?([^\s`*]+)`?")


def parse_finding_files(findings_text: str) -> set[str]:
    """Extract the filenames named in '**File:** <name>' lines of an audit report.

    Used by incremental auto-iterate to learn which files a round's findings
    touch, so remediation can rewrite only those instead of the whole design.
    """
    files: set[str] = set()
    for m in _FINDING_FILE.finditer(findings_text):
        name = m.group(1).strip().rstrip(".,;:)")  # drop trailing punctuation
        if name.lower() not in ("n/a", "none", "-", ""):
            files.add(name)
    return files


def read_specific_files(base_dir: str, filenames: set[str]) -> str | None:
    """Return ### File: blocks for only the named files (incremental remediation).

    Returns None if none of the named files exist or are readable.
    """
    from pathlib import Path

    root = Path(base_dir).resolve()
    sections: list[str] = []
    for name in sorted(filenames):
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not content.strip():
            continue
        ext = path.suffix.lstrip(".")
        sections.append(f"### File: {name}\n```{ext}\n{content.rstrip()}\n```")
    return "\n\n".join(sections) if sections else None


def terraform_validate(base_dir: str) -> dict:
    """Run `terraform validate` against the workspace and return diagnostics.

    Deterministic integration check: catches undefined resource references,
    missing required arguments, and bad attribute names — i.e. the "one
    specialist referenced another's resource that doesn't exist / wasn't wired
    up" class of bug that the compliance auditor does not look for.

    Returns {'ran': bool, 'ok': bool, 'errors': [{'file','summary','detail'}], 'note': str}.
    ran=False (with a note, ok=True) when it could not meaningfully run — no .tf
    files, terraform not installed, or init/parse failure — so callers skip
    rather than treat it as a finding. Uses -backend=false so it never touches
    remote state or cloud credentials.
    """
    import json
    import shutil
    from pathlib import Path

    root = Path(base_dir).resolve()
    if not root.is_dir() or not list(root.rglob("*.tf")):
        return {"ran": False, "ok": True, "errors": [], "note": "no terraform files"}
    if shutil.which("terraform") is None:
        return {"ran": False, "ok": True, "errors": [], "note": "terraform not installed"}

    # Match:  Error: Duplicate data "aws_caller_identity" configuration
    #   then:    on b.tf line 1:
    _INIT_ERROR_BLOCK = re.compile(
        r"Error:\s+(.+?)(?=\nError:|\Z)", re.DOTALL
    )
    _INIT_FILE_REF = re.compile(r"\bon\s+(\S+\.tf)\s+line\s+\d+")
    _DUPLICATE_SUMMARY = re.compile(
        r"Duplicate\s+(\w[\w ]+?)\s+\"([^\"]+)\"\s+\w+", re.IGNORECASE
    )

    try:
        init = subprocess.run(
            ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
            capture_output=True, cwd=base_dir, timeout=180,
        )
        if init.returncode != 0:
            stderr = init.stderr.decode("utf-8", "replace")
            # Terraform 1.x parses the config during init and surfaces config errors
            # (duplicate blocks, invalid syntax) before downloading providers.
            # Surface these as validate-style findings so the fixer can act on them.
            if "problems with the configuration" in stderr or re.search(
                r"Duplicate|already declared|Invalid", stderr
            ):
                errors = []
                # For duplicate errors, find ALL .tf files that declare the
                # offending block so the fixer sees the full picture and can
                # decide which file keeps it and which removes it.
                _DATA_DECL = re.compile(
                    r'^data\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE
                )
                _RES_DECL = re.compile(
                    r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE
                )
                from pathlib import Path as _Path
                tf_contents: dict[str, str] = {}
                for tf in sorted(_Path(base_dir).glob("*.tf")):
                    try:
                        tf_contents[tf.name] = tf.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass

                for block in _INIT_ERROR_BLOCK.findall(stderr):
                    block = block.strip()
                    if not block or block.startswith("Terraform encountered"):
                        continue
                    file_m = _INIT_FILE_REF.search(block)
                    fname  = file_m.group(1) if file_m else ""
                    dup_m  = _DUPLICATE_SUMMARY.search(block)
                    if dup_m:
                        kind, btype = dup_m.group(1).strip(), dup_m.group(2)
                        # Find every file that declares this block type so fixer
                        # can see all copies and resolve the conflict.
                        if kind.lower() == "data":
                            pattern = re.compile(
                                rf'^data\s+"{re.escape(btype)}"\s+"[^"]+"', re.MULTILINE
                            )
                        else:
                            pattern = re.compile(
                                rf'^resource\s+"{re.escape(btype)}"\s+"[^"]+"', re.MULTILINE
                            )
                        all_files = sorted(
                            f for f, txt in tf_contents.items() if pattern.search(txt)
                        )
                        file_list = ", ".join(all_files) if all_files else fname
                        summary = f'Duplicate {kind} "{btype}" — declared in: {file_list}'
                        detail  = (
                            f'"{btype}" is declared in multiple .tf files ({file_list}). '
                            "Keep it in exactly one file (typically the specialist that "
                            "primarily uses it) and remove all other declarations."
                        )
                        # Emit one finding per duplicate file so parse_finding_files
                        # picks them all up and the fixer receives all of them.
                        for f in (all_files or [fname]):
                            errors.append({"file": f, "summary": summary, "detail": detail})
                    else:
                        summary = block.splitlines()[0][:120]
                        detail  = block[:300]
                        errors.append({"file": fname, "summary": summary, "detail": detail})
                if errors:
                    return {"ran": True, "ok": False, "errors": errors, "note": ""}
            note = stderr.strip()[:300]
            return {"ran": False, "ok": True, "errors": [], "note": f"terraform init failed: {note}"}
        res = subprocess.run(
            ["terraform", "validate", "-json"],
            capture_output=True, cwd=base_dir, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ran": False, "ok": True, "errors": [], "note": f"terraform run error: {e}"}

    try:
        data = json.loads(res.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ran": False, "ok": True, "errors": [], "note": "could not parse validate output"}

    if data.get("valid", True):
        return {"ran": True, "ok": True, "errors": [], "note": ""}

    errors = []
    for d in data.get("diagnostics", []):
        if d.get("severity") != "error":
            continue
        errors.append({
            "file": (d.get("range") or {}).get("filename", ""),
            "summary": d.get("summary", ""),
            "detail": d.get("detail", ""),
        })
    return {"ran": True, "ok": not errors, "errors": errors, "note": ""}


def render_validation_findings(errors: list[dict]) -> str:
    """Format terraform validate errors as a findings block the remediation
    fixer can consume (the **File:** lines are picked up by parse_finding_files).
    """
    lines = [
        "## Terraform Validation Errors",
        f"{len(errors)} error(s) from `terraform validate` must be fixed:\n",
    ]
    for i, e in enumerate(errors, 1):
        lines.append(f"### Validation Error {i} — {e.get('summary', 'invalid configuration')}")
        if e.get("file"):
            lines.append(f"**File:** {e['file']}")
        lines.append("**Requirement:** configuration must pass `terraform validate`")
        lines.append(f"**Current state:** {e.get('summary', '')}".rstrip())
        if e.get("detail"):
            lines.append(f"**Required fix:** {e['detail']}")
        lines.append("")
    return "\n".join(lines)


_INCLUDE_EXTENSIONS = {
    '.tf', '.hcl',                          # Terraform
    '.py', '.js', '.ts', '.go',             # application code
    '.yaml', '.yml', '.json',               # config / data
    '.md', '.txt',                          # docs and audit findings
}
_SKIP_DIRS = {'.git', '.terraform', '__pycache__', 'node_modules', '.venv', 'venv'}
_SKIP_PATTERNS = ('*.tfstate', '*.tfstate.backup', '*.tfvars', '*.tfvars.json')
_CONTEXT_CAP = 200_000  # chars — prevents flooding the context window


def read_workspace_context(base_dir: str) -> str | None:
    """Scan the workspace and return existing file contents in ### File: format.

    Returns None if the workspace is empty (new project). Skips secrets-risk
    files (*.tfvars), state files (*.tfstate*), VCS/vendor directories, and
    anything that isn't plain text.
    """
    import fnmatch
    from pathlib import Path

    root = Path(base_dir).resolve()
    if not root.is_dir():
        return None

    sections: list[str] = []
    total_chars = 0

    for path in sorted(root.rglob('*')):
        # Skip hidden/vendor directories anywhere in the tree
        if any(part in _SKIP_DIRS or part.startswith('.') for part in path.parts[len(root.parts):]):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in _INCLUDE_EXTENSIONS:
            continue
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat.lstrip('*')) or
               fnmatch.fnmatch(path.name, pat.lstrip('*'))
               for pat in _SKIP_PATTERNS):
            continue

        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue

        if not content.strip():
            continue

        ext = path.suffix.lstrip('.')
        block = f"### File: {rel}\n```{ext}\n{content.rstrip()}\n```"
        if total_chars + len(block) > _CONTEXT_CAP:
            sections.append(
                f"### Note: workspace context truncated at {_CONTEXT_CAP:,} chars "
                f"to stay within context limits."
            )
            break
        sections.append(block)
        total_chars += len(block)

    if not sections:
        return None

    header = (
        f"## Existing workspace ({len(sections)} file(s))\n\n"
        "The following files already exist in the working directory. "
        "This is an INCREMENTAL task — preserve all existing resources, "
        "and only add or modify what the new request requires.\n\n"
        "Pay special attention to any `audit_findings.md` or similar findings "
        "files — treat every listed finding as a hard requirement to address.\n"
    )
    return header + "\n\n".join(sections)


def write_findings(crew_result, base_dir: str, framework: str) -> dict:
    """Write the auditor's findings report to audit_findings.md and commit it.

    Returns {'file': str, 'error': str | None}.
    """
    from datetime import datetime

    report = str(getattr(crew_result, "raw", "") or crew_result).strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = f"<!-- {framework} audit — {timestamp} -->\n\n{report}\n"

    findings_path = os.path.join(base_dir, "audit_findings.md")
    try:
        with open(findings_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"file": "audit_findings.md", "error": f"write failed: {e}"}

    try:
        subprocess.run(["git", "add", "audit_findings.md"],
                       check=True, capture_output=True, cwd=base_dir)
        subprocess.run(["git", "commit", "-m", f"Add {framework} audit findings"],
                       check=True, capture_output=True, cwd=base_dir)
    except subprocess.CalledProcessError as e:
        return {
            "file": "audit_findings.md",
            "error": f"git failed: {e.stderr.decode('utf-8', errors='replace').strip()}",
        }

    return {"file": "audit_findings.md", "error": None}


def _read_disk_tf(base_dir: str) -> list[str]:
    """Return the text of every Terraform resource file on disk.

    Excludes the generated variables.tf / outputs.tf themselves and vendor dirs,
    so derived files can be regenerated from the full, current resource set.
    """
    from pathlib import Path

    root = Path(base_dir).resolve()
    if not root.is_dir():
        return []
    texts: list[str] = []
    for path in sorted(root.rglob('*')):
        if any(part in _SKIP_DIRS or part.startswith('.')
               for part in path.parts[len(root.parts):]):
            continue
        if not path.is_file() or path.suffix.lower() not in ('.tf', '.hcl'):
            continue
        if path.name in ('variables.tf', 'outputs.tf'):
            continue
        try:
            texts.append(path.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
    return texts


def commit_audited_output(crew_result, *, has_auditor: bool = True,
                          allowed_files: set[str] | None = None) -> dict:
    """Parse the crew output, write each emitted file, regenerate derived
    Terraform files from disk, and create a single git commit.

    When has_auditor=True (default), every task output is scanned: the
    specialists' files are collected and the FINAL task (auditor or remediation
    fixer) overwrites any file it re-emitted. This lets the auditor emit only
    the files it actually changed — unchanged specialist files flow through
    untouched, instead of the auditor re-emitting (and re-paying output tokens
    for) the entire set.

    When has_auditor=False, all task outputs are merged and same-named files
    from different specialists are concatenated (no auditor to consolidate).

    variables.tf and outputs.tf are always regenerated in pure Python from the
    full set of resource files on disk after the emitted files are written, so
    they stay correct whether this was a full generation or an incremental
    remediation that only rewrote a couple of files. An explicitly-emitted
    variables.tf / outputs.tf is respected and not overwritten.

    When allowed_files is given (incremental remediation), only files whose name
    is in that set are written — any extra file the fixer emitted but was not
    asked to touch is discarded, so an unaffected file is never overwritten with
    a from-scratch regeneration. Derived files are still regenerated from disk.

    Returns {'files': list[str], 'commit_message': str, 'error': str | None}.
    """
    base_dir = os.environ.get("PROJECT_WORKSPACE_DIR", ".")

    tasks_output = getattr(crew_result, "tasks_output", None)
    if tasks_output:
        texts = [str(getattr(t, "raw", "") or t) for t in tasks_output]
    else:
        texts = [str(getattr(crew_result, "raw", "") or crew_result)]

    # Truncated blocks (response cut off at max_tokens) are skipped so a partial
    # file never overwrites a complete one; the prior version stays on disk and
    # the next audit round can re-attempt the fix.
    truncated: list[str] = []
    merged: dict[str, str] = {}
    if has_auditor:
        # All tasks except the last are specialists (the architect emits no
        # files); the last task is the auditor/fixer and its files overwrite.
        specialist_texts = texts[:-1]
        final_text = texts[-1]
        for text in specialist_texts:
            for filename, content, closed in _iter_file_blocks(text):
                if not closed:
                    truncated.append(filename)
                    continue
                if filename in merged:
                    merged[filename] += _sep(filename) + content
                else:
                    merged[filename] = content
        for filename, content, closed in _iter_file_blocks(final_text):
            if not closed:
                truncated.append(filename)
                continue
            merged[filename] = content  # auditor's fixed version wins
    else:
        # No auditor: merge all specialists, concatenating same-named files.
        for text in texts:
            for filename, content, closed in _iter_file_blocks(text):
                if not closed:
                    truncated.append(filename)
                    continue
                if filename in merged:
                    merged[filename] += _sep(filename) + content
                else:
                    merged[filename] = content

    # Incremental remediation: discard any file the fixer emitted that it wasn't
    # asked to touch, so an unaffected file is never overwritten from scratch.
    ignored: list[str] = []
    if allowed_files is not None:
        ignored = [k for k in merged if k not in allowed_files]
        merged = {k: v for k, v in merged.items() if k in allowed_files}

    if not merged:
        msg = "No complete in-scope '### File:' blocks found in any task output"
        if truncated:
            msg += f" (skipped truncated: {', '.join(sorted(set(truncated)))})"
        return {"files": [], "commit_message": "", "error": msg}

    # Audit notes come from the last text that has them (auditor's output).
    audit_notes = ""
    for text in reversed(texts):
        m = _AUDIT_NOTES.search(text)
        if m:
            audit_notes = m.group(1).strip()
            break

    written: list[str] = []
    for filename, content in merged.items():
        path = os.path.abspath(os.path.join(base_dir, filename))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        written.append(filename)

    # Regenerate derived Terraform files from the full on-disk resource set
    # (pure Python — no LLM). Skip any the model emitted explicitly.
    disk_tf = _read_disk_tf(base_dir)
    for derived, generator in (("variables.tf", generate_variables),
                               ("outputs.tf", generate_outputs)):
        if derived in merged:
            continue
        content = generator(disk_tf)
        if content:
            with open(os.path.join(base_dir, derived), "w") as f:
                f.write(content)
            if derived not in written:
                written.append(derived)

    summary = (f"Add {', '.join(written)}" if len(written) <= 3
               else f"Generated {len(written)} files")
    message = f"{summary}\n\nAudit Notes:\n{audit_notes}" if audit_notes else summary

    try:
        subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=base_dir)
        subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True, cwd=base_dir)
    except subprocess.CalledProcessError as e:
        return {"files": written, "commit_message": message,
                "error": f"git failed: {e.stderr.decode('utf-8', errors='replace').strip()}"}

    # Good files were committed; flag any truncated ones so the caller knows the
    # prior version was kept and the fix should be re-attempted next round.
    warning = (f"skipped truncated file(s): {', '.join(sorted(set(truncated)))} "
               "(response hit token limit; kept prior version)") if truncated else None
    return {"files": written, "commit_message": message, "error": warning}


@tool("Git Commit Tool")
def git_commit_tool(commit_message: str) -> str:
    """Stages all modified files and commits them to the local git repository.
    
    Args:
        commit_message: A short, descriptive commit message.
    
    Note: The project workspace directory must already be a git repository.
    Run 'git init' in that directory first if it isn't.
    """
    try:
        base_dir = os.environ.get("PROJECT_WORKSPACE_DIR", ".")
        subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=base_dir)
        subprocess.run(["git", "commit", "-m", commit_message], check=True, capture_output=True, cwd=base_dir)
        return f"Success: Committed changes in '{base_dir}' with message: '{commit_message}'"
    except subprocess.CalledProcessError as e:
        return f"Git error: {e.stderr.decode('utf-8')}. Make sure the directory is a git repository (run 'git init' first)."
