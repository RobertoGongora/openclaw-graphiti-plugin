# Memory reliability and evidence measurements

These changes address failures from the blind user-profile audit. Measurements
are observations from this run, not production performance or accuracy guarantees.

## Payload and file comparison

The original profile run used 10 calls, 12.598 seconds of summed tool latency and
57,327 response bytes. For the same 14 source-reviewed facts, fact-array sizes are:

| Evidence view | Bytes | Reduction from full |
| --- | ---: | ---: |
| Full | 34,288 | — |
| Compact | 17,853 | 48% |
| Index | 10,709 | 69% |

These sizes exclude the protocol envelope and expansion guidance. Compact is the
new MCP default; index lets an agent choose IDs for batched full expansion.
Summaries are capped at 240 characters and quote excerpts at 400. Full evidence
is required for sourced writes; excerpts do not pretend to be exact quotes.

A local inventory of Claude project memory and Codex memory Markdown files,
deduplicated by filesystem identity, found 848 files totaling 3,958,408 bytes.
The median was 2,827.5 bytes, p90 7,491 bytes, and largest 305,217 bytes.
A warm raw read of the largest file took a median 0.0219 milliseconds. That excludes
file discovery, agent transport, interpretation and evidence checking. Thus 57 KB
is reasonable for a broad sourced profile, but exceeds most individual memory
files; graph retrieval is not inherently faster than reading a known local file.

## Historical query memory

A 50,002-node synthetic checkpoint, measured in three fresh processes per mode,
used 86,542,628 peak Python allocation bytes for full restore versus 19,162,717
for selective restore (about 78% less). Median restore times were 0.541 and 0.434
seconds respectively. This excludes database, network and delta replay costs.

Entity and evidence history selectively retain nodes while checking the entire
journal. General historical recall/search still reconstruct namespace state.
A process-shared lock serializes historical MCP handlers, with BUSY responses to
contenders; live calls remain available. This bounds simultaneous reconstruction,
but does not establish that idle allocator retention or all OOM causes are solved.

## Corrections and dreaming

The user-requested identities `person:user` and `person:session-user` were merged
into `person:rob` in live memory, preserving aliases and history. Code also gives
canonical keys precedence over copied aliases and restricts kind-qualified recall.

The two DigitalOcean “MCP or API” facts were checked against their original source
and corrected in live memory: only their erroneous exclusive slots were cleared.
Their claims, planned status and citations survived. The reusable correction tool
requires the complete unretracted role history to avoid reviving ended records.

Optional `dream --review-uncertain` now reviews up to ten uncertain claims against
eligible original user statements and fresh successful tool results. Reviews retain
source IDs and supported/contradicted/insufficient recommendations separately from
facts. Applying a dream never promotes these claims or marks them user-confirmed.
It is opt-in and not scheduled automatically.

Three synthetic model probes produced the three expected review labels. That is
a smoke test, not an accuracy estimate. Alternative-extraction probes had inconsistent
coverage, including empty outputs, so the added prompt guidance has no demonstrated
prevention benefit yet. The specific live conflict correction is independently verified.

## Validation and remaining work

The final code passed 406 deterministic tests with one skipped, all six real-model
golden cases, Ruff, Pyright and shared-skill validation. Tests cover historical
integrity failures, cross-process admission and process death, role-history correction,
alias collisions, evidence truncation, and non-promoting dream review.

Remaining research includes representative production historical memory measurements,
retained session memory, broader claim-review precision/coverage, and extraction
coverage for compatible alternatives. Server and skill changes require deployment;
only the explicitly requested live identity and fact corrections have been applied.

Machine-readable measurements: [20260924-memory-reliability.json](20260924-memory-reliability.json).
