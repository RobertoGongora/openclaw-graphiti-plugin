# ADR 009: Question-ranked detail and memory-derived report provenance

Status: implemented on branch; validation and deployment are recorded in the PR.

## Problem and evidence

Three memory-only agents answered nine questions through 27 recall calls. The
compact evaluation-results query returned progress reports while excluding the
final measurements. A production-deployment query returned an unrelated note.
Full recall ignored the question because ADR 005 preserved the old raw projection.
Uncertain records with the same endpoints were collapsed to the newest wording,
which could discard distinct details or corrections.

The transcript worker also ingested the agents' memory-based answers. Subsequent
recall returned those summaries with larger repetition/session counts. Their
status remained uncertain, but they were not independent sources, and shorter
new summaries could displace richer original reports.

## Decisions

Question relevance is shared by compact and full detail. A question selects the
same fact IDs, pagination and history scope; full expands the selected records.
Full without a question retains the legacy diagnostic lane view. There is no
retrieval-time model call. Rank individual facts rather than broad report roles,
keep partial matches available, and inherit relevance only within real exclusive
state roles so an old database name still surfaces its replacement/conflict.
Because partial matches stay, the conflict status needs a threshold: it reports a
disagreement only when one matches as strongly as the best result, so the page
never claims a dispute it does not show. Weaker matching disagreements remain
visible as a count and as evidence-ready IDs, both sides included.
Weighted lexical matching remains imperfect; measured-outcome and schema-concept
features are transparent heuristics, not semantic understanding.

Keep differently worded uncertain claims. Only identical wording is grouped,
and its counts are explicitly reports/sessions rather than independent support.
Source-message time orders otherwise comparable reports; ingestion time does
not. Expose that time separately as reported_at without changing the event-time
projection, manufacturing dates, deleting old evidence, or declaring uncertain
corrections verified. Explicitly contradictory reports still need verification
or an explicit correction/retraction; chronology alone is not proof. Dated records
order by event time before report time; the two are never compared with each
other. A direct MCP write only contributes report time through verified source
messages, never through a timestamp the caller chose for an unverified message.
Live and historical reads derive a missing reported_at from evidence messages in
the same UTC form.

Keep main-agent and sub-agent transcripts. Agent identity is not a proxy for
novelty: sub-agents can discover independently observed facts, and the main agent
can repeat memory. Track memory/delegation read results and available original
fact IDs as transcript-level memory_origins for subsequent assistant reports in
the same user turn. Compute this from the complete parsed session before batching,
so a read far before the report is not forgotten. Preserve parser output and
prefix hashes unchanged. Persist origin references alongside source messages and
expose them in evidence for any independently corroborated new claim.

A report based on those reads cannot create another fact without fresh non-memory
tool evidence for the specific new claim. Pure memory-report batches skip the
model and complete with zero new facts. The validator enforces the same rule even
if a caller submits an uncertain extraction or cites old user context. New user
assertions/corrections and fresh tool-backed findings remain eligible. Model
instructions must still judge whether the fresh evidence supports that exact
claim; deterministic checks establish source eligibility, not semantic truth.

Direct memory reads, evidence reads, reads through any memory tool the parser
labels (other memory servers included) and literal nested tools.memory_* calls
are recognized, as are delegation results by exact tool name: Codex spawn_agent,
wait_agent, send_message and followup_task, and Claude Code Task and Agent. A
memory-bank note is a .md or .txt file at any depth under a memory directory;
code whose path contains the word memory, a process-memory statistics tool, or a
chat tool whose name contains send_message does not taint a report, even where
the parser's broader labels hold such output back from validation. Every
user-role message starts a turn, including a delegated or automated instruction,
both for origins and for the tool results carried as batch context, so a
sub-agent's later finding is not tainted by an earlier read. Opaque/dynamic
programs and memory paraphrases whose origin is
absent from the source cannot always be recognized. Do not claim complete
semantic novelty detection. Origin IDs are bounded; unparseable result content
still marks the report as memory-derived, but may lack original fact IDs. The
model view carries only the read IDs present in the batch and labels memory and
delegation results as memory reads; the recalled fact IDs stay in the stored
payload. A transcript without reads serializes exactly as before, so episode IDs
and the daemon's change fingerprints do not change on upgrade.

## Existing knowledge and validation

This change does not rewrite historical facts or source payloads. Existing echoes
need a separately reviewed evidence audit; re-extracting every completed episode
is not part of this fix. Read-time wording preservation prevents those echoes
from silently replacing a richer record. Status counts explicitly warn that
separate sessions can share recalled evidence.

The extraction engine fingerprint changes. Apply normal engine-release backup,
journal-verification and rollout checks before deployment. Cursor compatibility
is covered by a long-session integration regression.

Use fixed private recall projections for development comparisons. Offline evals
never consult or write to the live graph, so evaluation output cannot alter their
inputs. Keep private text out of committed reports. The nine development questions
include eight source-reviewed answer records and one unscored missing-rationale
question; improvements on these cases are regression evidence, not an independent
accuracy estimate. Follow up with unseen questions after deployment, with expected
answers reviewed against their sources, never taken from live recall: the study
sessions that ask the questions are themselves ingested.
