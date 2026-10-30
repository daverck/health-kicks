"""Single source of truth for activities supported by the on-device Edge AI model."""

EDGE_AI_LABELS: frozenset[str] = frozenset(
    {
        "idle",
        "walk",
        "run",
        "stairs_up",
        "stairs_down",
        "sit_down",
        "stand_up",
        "lie_down",
        "lying_supine",
        "lying_prone",
        "lying_lateral",
        "jump",
        "fall_forward",
        "fall_backward",
        "fall_lateral",
        "fall_recovery",
        "fall_generic",
    }
)


def edge_ai_labels_sorted() -> list[str]:
    """Return the Edge AI labels as a deterministic, sorted list."""
    return sorted(EDGE_AI_LABELS)