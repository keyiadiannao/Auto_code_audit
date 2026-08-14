# Security Policy

## What this tool does — and does not do

Auto Code Audit is a **static analyzer**: its scanners parse Python with
`ast.parse` and analyze the resulting syntax tree. It **never imports,
executes, or installs target-project code**. The benchmark harness
(`benchmarks/run_benchmarks.py`) also clones but never executes the target
repositories.

If you point it at untrusted code, the risk profile is bounded by the Python
parser and the tool's own code — not by arbitrary code execution in the target.

## Reporting a vulnerability

Please do **not** open a public issue for a security-sensitive bug in the tool
itself (e.g. a scanner crash that could be triggered by crafted input, or a
case where the tool unexpectedly executes code). Report it privately by opening
an issue with a minimal reproducer and note that it is security-sensitive; the
maintainer will triage it first.

## Scope

- The current `main` branch.
- Security-relevant behavior includes: any path where the tool executes target
  code, any path where crafted input causes arbitrary code execution in the
  *tool* process, and any unsafe deserialization of reports or verdicts.

Reports of bugs that require the user to already trust their own codebase are
appreciated but are not treated as security vulnerabilities.
