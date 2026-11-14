"""Fate pressure boxes and compatibility with earlier saves."""

CAPACITIES = {'mild': 2, 'moderate': 4, 'severe': 6}


def stress_boxes(attributes):
    tracks = attributes.get('stress_tracks') or {}
    if 'unified' in tracks:
        return tracks['unified']
    if tracks:
        return [any(index < len(track) and track[index] for track in tracks.values())
                for index in range(2)] + [any(any(track[2:]) for track in tracks.values())]
    return None
