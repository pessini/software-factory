# Repository and evidence protection

Issue [#2](https://github.com/pessini/software-factory/issues/2) replaces automatic
restoration with detection and preservation. This policy is version 2.

## Repository scope

A run starts at the repository root with a clean index and worktree. If existing
staged, unstaged, or untracked work is present, bootstrap refuses the run and
leaves that work intact. It does not stash, reset, clean, or commit anything.

`protection.json` records the starting commit, current branch reference, all
references, starting file names, workspace, and permitted change scope. Builder
may change non-ignored application and test files. `.factory/`, `.bb/`, and every
configured protected path are always protected. Builder must not change the Git
index, HEAD, or any repository reference. Planner and Reviewer must have
read-only access to all tracked, untracked, and ignored workspace files.

Checks compare file hashes, file type, permission bits, symbolic-link targets,
index entries and flags, HEAD, and repository references. The saved reference,
index, and protected-file baseline is checked between steps and on resume. Protected directory checks also
include ignored files and missing paths. A changed reference in another worktree
stops the run because its owner is unknown. The check preserves the branch,
commit, index, and files exactly as observed. It never restores all references
or attributes an unexplained change to the child. The failure report identifies
the affected paths when available and leaves the workspace for inspection.

File checks stream SHA-256 hashes. They retain no file contents or backups and
never write Git objects. Credentials and dependency caches are not copied into
Git or controller storage. Ignored files remain in the read-only check scope;
there is no cache or credential exception to protection. Large ignored trees can
therefore increase check time. Existing Git objects are not removed.

Checks detect lasting changes at operation boundaries. They cannot prevent writes
or detect a change that was fully undone before a check. They supplement enforced
child isolation; they do not replace it. Exclusive run and workspace ownership is
separate work in issue #3.

## Controller evidence

Authoritative artifacts are stored under
`$BB_THREAD_STORAGE/factory/artifacts/<run-id>/`, outside the workspace. The
controller records resolved configuration, request, plan, repository protection
scope, command logs/results, build reports, and review outputs there. Children
receive absolute paths for permitted reads. The controller reads repair and
revision evidence through the verified store and loads saved configuration on
resume.

`manifest.json` has schema version 2, run/workspace identity, and a revision number
and SHA-256 hash for each artifact. The running controller holds the expected
manifest in memory. Reads, writes, child results, correction requests, and final
acceptance check the manifest and files. Missing files, unexpected files,
changed contents, changed manifests, symbolic links, and special files such as
named pipes cause rejection without opening a blocking stream.
Command output is collected in a temporary file and then recorded by the
controller. A corrupt store is preserved; failure details go to controller events
instead of overwriting damaged evidence with a final report.

Directories use mode 0700 and files use mode 0600. These modes exclude other
operating-system users. **They do not exclude a child process running as the same
user.** The manifest on disk is trusted only while child isolation prevents
writes to the controller's complete storage, including checkpoints and events.
There is no claim of protection against a compromised controller or host.

Old runs with only `.factory/runs/<run-id>/` artifacts are rejected on resume.
Missing or unsupported manifests also cause rejection; no automatic migration
or reconstruction from child-writable files occurs. A crash between an artifact
write and its manifest update can require a new run. Preserve old or damaged
files for inspection and start a new run after resolving the cause.

Binding final approval to the exact tested application contents remains in issue
#1. Artifact integrity alone does not establish that binding.

## Provider enforcement and current limitation

| Adapter or mode | Established behavior | Factory policy |
| --- | --- | --- |
| BB `auto` | Workspace edits are allowed; the factory has no verified control to enforce read-only roles or protect controller storage. | Refuse live execution before dispatch. |
| BB `accept-edits` | Workspace edits are allowed; approval handling is not a read-only boundary. | Refuse live execution before dispatch. |
| BB `full` | Existing factory configuration rejects it. | Remains forbidden. |
| Scripted test adapter | Runs known test actions in process. It deliberately permits tampering to test detection. | Automated tests only; not a supported live provider. |

These rows describe the generic BB CLI adapter. The default adapter now uses
[the protected local worker](protected-worker.md): a container per provider
operation, a read-only Planner/Reviewer workspace, read-only existing protected
Builder paths and Git metadata, and controller evidence outside writable mounts.
New protected-path creation is detected and rejected after the operation.

Local container tests cover writes, deletion, rename, permission changes, and
link-based attempts. The authenticated demo run, crash/recovery/cancel check,
and same-Builder follow-up passed. Their
[saved evidence](live-worker-acceptance.json) includes exact provider turn IDs
and confirmed container exit. Other provider/environment combinations remain
unsupported and fail prerequisite checks.

Automated regression tests prove preservation of another worktree's commit and
new branch, preservation of existing staged and unstaged work, absence of new
Git objects containing ignored secrets/caches, rejection of forged plans and test
results, and valid simulated implementation, repair, review, and resume. A
separate-process test proves that location and ordinary file permissions do not
prevent same-user evidence replacement. A dispatch test proves that unverified
BB permissions are refused. Actual provider isolation is now covered by the local and authenticated worker
checks described above.
