-- Preserve used positions from either track. The new track has exactly three boxes.
UPDATE entities
SET attributes = json_set(
    attributes, '$.stress_tracks', json_object('unified', json_array(
        json(CASE WHEN json_extract(attributes, '$.stress_tracks.physical[0]') OR json_extract(attributes, '$.stress_tracks.mental[0]') THEN 'true' ELSE 'false' END),
        json(CASE WHEN json_extract(attributes, '$.stress_tracks.physical[1]') OR json_extract(attributes, '$.stress_tracks.mental[1]') THEN 'true' ELSE 'false' END),
        json(CASE WHEN json_extract(attributes, '$.stress_tracks.physical[2]') OR json_extract(attributes, '$.stress_tracks.mental[2]') OR json_extract(attributes, '$.stress_tracks.physical[3]') OR json_extract(attributes, '$.stress_tracks.mental[3]') THEN 'true' ELSE 'false' END)
    ))
)
WHERE json_type(attributes, '$.stress_tracks') = 'object'
  AND json_type(attributes, '$.stress_tracks.unified') IS NULL;
