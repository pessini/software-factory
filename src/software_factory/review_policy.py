"""Select review scope from controller-owned patches and the saved project policy."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field

from software_factory.config import ReviewPolicy
from software_factory.contracts.models import ContentVersion, Contract, Reviewer

INSTRUCTION_PATHS = (
    "*deploy*",
    ".env*",
    ".github/workflows/",
    ".github/actions/",
    ".gitlab-ci*",
    "Jenkinsfile*",
    "Dockerfile*",
    "*.dockerfile",
    "compose*.y*ml",
    "docker-compose*.y*ml",
    "*.tf",
    "*.tfvars",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock*",
    ".npmrc",
    "pyproject.toml",
    "uv.lock",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile*",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile*",
    "go.mod",
    "go.sum",
    "pom.xml",
    "*.gradle*",
    "gradle.lockfile",
    "composer.json",
    "composer.lock",
    "Makefile*",
    "justfile",
    "*.sh",
    "*.bash",
    "*.ps1",
    "AGENTS.md",
    "CLAUDE.md",
    ".factory/",
    "tools/",
    "scripts/",
    ".gitmodules",
    "*.csproj",
    "*.fsproj",
    "Directory.Build.*",
    "Directory.Packages.props",
)
# These rules cannot be removed by a project's generated/documentation patterns.
SENSITIVE_PATHS = (
    *INSTRUCTION_PATHS,
    "*auth*",
    "*secret*",
    "*credential*",
    "*password*",
    "*crypto*",
    "*encrypt*",
    "*permission*",
    "*session*",
    "*token*",
    "*migration*",
    "*.pem",
    "*.key",
)
SENSITIVE_CONTENT = re.compile(
    r"auth(?:entication|orization|enticate|orize)?|secret|credential|password|"
    r"cryptograph|encrypt|decrypt|migration|\bdeploy\b|\bsubprocess\b|"
    r"child_process|\b(?:exec|eval)\s*\(|\bos\.system\b|\b(?:pickle|yaml)\.load\b|"
    r"\b(?:preinstall|postinstall|install_requires|jwt|oauth|hmac|hashlib|token|"
    r"session|permission|rbac|acl)\b",
    re.IGNORECASE,
)
INSTRUCTION_CONTENT = re.compile(
    r"\b(?:getenv|environ|process\.env|subprocess|child_process|argparse|typer|"
    r"commands?|cli|tools?)\b|\$\{?[A-Z][A-Z0-9_]+|--[a-z][a-z-]+",
)


class ReviewSelection(Contract):
    version: Literal[1] = 1
    content_version: ContentVersion
    policy: ReviewPolicy
    level: Literal["trivial", "normal", "full"]
    reasons: list[str] = Field(min_length=1)
    required_reviewers: list[Reviewer]
    changed_lines: int
    changed_file_count: int
    measured_file_count: int
    sensitive_files: list[str]
    unmeasurable_files: list[str]
    generated_files: list[str]
    review_files: list[str]
    instruction_reasons: list[str]


def matches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    path = path.casefold()
    parts = PurePosixPath(path).parts
    for raw in patterns:
        pattern = raw.casefold()
        if pattern.endswith("/"):
            directory = pattern.rstrip("/")
            if any(fnmatchcase("/".join(parts[:i]), directory) for i in range(1, len(parts) + 1)):
                return True
            if "/" not in directory and any(fnmatchcase(p, directory) for p in parts):
                return True
        elif "/" not in pattern:
            if any(fnmatchcase(part, pattern) for part in parts):
                return True
        elif fnmatchcase(path, pattern) or (
            pattern.startswith("**/") and fnmatchcase(path, pattern[3:])
        ):
            return True
    return False


def patch_changes(patch: str) -> tuple[list[str], bool, str]:
    """Count hunk lines, including source lines which start with +++ or ---."""
    lines = []
    context = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@ "):
            in_hunk = True
            context.append(line)
        elif line.startswith("diff --git "):
            in_hunk = False
        elif in_hunk and line.startswith(("+", "-", " ")):
            context.append(line[1:])
            if line.startswith(("+", "-")):
                lines.append(line[1:])
    unmeasurable = (
        "GIT binary patch" in patch
        or "Binary files " in patch
        or re.search(
            r"(?m)^(?:new file mode|deleted file mode|old mode|new mode) (?:120000|160000)$", patch
        )
        or (bool(patch.strip()) and not lines)
    )
    return lines, bool(unmeasurable), "\n".join(context)


def select_review(
    policy: ReviewPolicy,
    patches: dict[str, str],
    content_version: str,
    starting_files: dict[str, Any],
    current_files: dict[str, Any],
) -> ReviewSelection:
    sensitive, unknown, generated, primary, instruction_reasons = [], [], [], [], []
    changed_lines = 0
    docs_changed = False
    for path, patch in patches.items():
        lines, unmeasurable, context = patch_changes(patch)
        sensitive_change = (
            matches(path, (*SENSITIVE_PATHS, *policy.sensitive_paths))
            or SENSITIVE_CONTENT.search(context) is not None
            or bool(
                re.search(
                    r"(?m)^(?:new file mode|deleted file mode|old mode|new mode) (?:100755|160000)$",
                    patch,
                )
            )
        )
        if sensitive_change:
            sensitive.append(path)
        if matches(path, policy.generated_paths) and not sensitive_change:
            generated.append(path)
            continue
        primary.append(path)
        changed_lines += len(lines)
        if unmeasurable:
            unknown.append(path)
        docs_changed |= matches(path, policy.documentation_paths)
        if matches(path, INSTRUCTION_PATHS) or INSTRUCTION_CONTENT.search(context) is not None:
            instruction_reasons.append(f"Tools, commands, environment, or project rules: {path}")

    def directories(files: dict[str, Any]) -> set[str]:
        return {
            str(parent)
            for path, entry in files.items()
            if entry["kind"] != "missing" and path not in generated
            for parent in PurePosixPath(path).parents
            if str(parent) != "."
        }

    if directories(starting_files) != directories(current_files):
        instruction_reasons.append("Repository directory structure changed")
    reasons = []
    if sensitive or unknown:
        level: Literal["trivial", "normal", "full"] = "full"
        if sensitive:
            reasons.append("Sensitive paths or changed content require security review")
        if unknown:
            reasons.append("Some changes cannot be measured as text lines")
    elif not patches or (
        bool(primary)
        and all(matches(path, policy.documentation_paths) for path in patches)
        and changed_lines <= policy.trivial_max_lines
        and len(primary) <= policy.normal_max_files
    ):
        level = "trivial"
        reasons.append("No changes" if not patches else "Documentation within the trivial limits")
    elif changed_lines <= policy.normal_max_lines and len(primary) <= policy.normal_max_files:
        level = "normal"
        reasons.append("Both normal size limits are satisfied")
    else:
        level = "full"
        reasons.append("The normal line or file limit is exceeded")
    reviewers: list[Reviewer] = ["request"]
    if level != "trivial":
        reviewers.append("code")
    if level == "full":
        reviewers.append("security")
    if docs_changed or instruction_reasons:
        reviewers.append("documentation")
        reasons.append("Documentation or project instructions need review")
    return ReviewSelection(
        content_version=content_version,
        policy=policy,
        level=level,
        reasons=reasons,
        required_reviewers=reviewers,
        changed_lines=changed_lines,
        changed_file_count=len(patches),
        measured_file_count=len(primary),
        sensitive_files=sensitive,
        unmeasurable_files=unknown,
        generated_files=generated,
        review_files=primary,
        instruction_reasons=instruction_reasons,
    )
