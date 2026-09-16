// Open http://127.0.0.1:17474/browser/ and connect to bolt://127.0.0.1:17687.
// Local development database: authentication is disabled.
// Run each query separately. Select the Graph result view for paths.
// The personal memory bank is being extracted by persistent background workers.

// 1. Atlas relationships. Fact nodes carry relation, status, dates and evidence.
// This is raw stored evidence: plans/history/uncertainty coexist here.
// Use memory_recall for the engine's resolved current-state view.
MATCH p=(s:MemoryEntity {namespace:'personal',key:'project:atlas'})
  -[:HAS_FACT]->(f:MemoryFact)-[:TARGET]->(t:MemoryEntity)
WHERE f.namespace=s.namespace AND t.namespace=s.namespace
  AND coalesce(f.retracted,false)=false
RETURN p
LIMIT 100;

// 2. Show the source episodes supporting Atlas's facts.
MATCH p=(s:MemoryEntity {namespace:'personal',key:'project:atlas'})
  -[:HAS_FACT]->(f:MemoryFact)-[:SUPPORTED_BY]->(e:MemoryEpisode)
WHERE f.namespace=s.namespace AND e.namespace=s.namespace
RETURN p
LIMIT 100;

// 3. Extracted entity/fact relationships in your memory bank.
MATCH p=(s:MemoryEntity {namespace:'personal'})
  -[:HAS_FACT]->(f:MemoryFact)-[:TARGET]->(t:MemoryEntity)
WHERE f.namespace=s.namespace AND t.namespace=s.namespace
RETURN p
LIMIT 200;

// 4. Intake progress. Staged source text is not extracted graph knowledge.
MATCH (e:MemoryEpisode {namespace:'personal'})
RETURN e.status AS status, count(*) AS episodes
ORDER BY status;

// 5. Live ingestion progress, separating active work from queued source text.
MATCH (e:MemoryEpisode {namespace:'personal'})
RETURN CASE WHEN e.status='complete' THEN 'complete'
  WHEN coalesce(e.lease_until,0)>timestamp()/1000.0 THEN 'processing'
  WHEN e.status='failed' THEN 'retrying' ELSE 'queued' END AS status,
  count(*) AS jobs
ORDER BY status;
