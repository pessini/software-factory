# Legacy runtime retirement

The Python/LangGraph application, its packaging, tests, and CI workflow have been
removed. Software Factory is the Rust/ADK service and native CLI/ACP/MCP clients.
See [startup](startup.md) for supported operations.

This removes code, not saved user data. Rust does not interpret LangGraph
checkpoints. To recover historical runs, use an isolated checkout and environment
from before retirement (for example commit `7b1dda2`), with the original storage,
workspace and provider context. Stop the Rust controller before using that
historical environment against the same workspace. Never copy old checkpoints
into Rust storage or relabel their evidence as Rust execution.

Historical acceptance documents describe their tested releases and may reference
removed Python commands. They are not current installation instructions.
Production cutover remains tracked separately in #27.
