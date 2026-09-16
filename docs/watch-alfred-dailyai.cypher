// Watch Alfred, DailyAI, and Unearth without assuming their relationship.
MATCH (entity:MemoryEntity {namespace:'personal'})
WHERE any(term IN ['alfred', 'dailyai', 'unearth']
  WHERE any(label IN [entity.name, entity.key] + coalesce(entity.aliases, [])
    WHERE replace(toLower(coalesce(label, '')), ' ', '') CONTAINS term))
OPTIONAL MATCH p=(entity)-[:HAS_FACT|TARGET*1..2]-(neighbor)
WHERE all(n IN nodes(p) WHERE n.namespace = 'personal')
  AND none(n IN nodes(p) WHERE n:MemoryFact AND coalesce(n.retracted, false))
RETURN entity, p
LIMIT 300;
