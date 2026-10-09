"""Resolve the effective flag values for one model: defaults, global, detection, overrides.

A model's saved profile holds only its *overrides* - values that differ from what it
would inherit - so a change to the global defaults reaches every model that has not
deliberately overridden that setting. (Profiles used to store a full snapshot of every
flag, which froze each edited model at the globals of the day.)
"""
from __future__ import annotations

from typing import Any

from . import flags as flagmod
from . import scanner

# Values that describe one specific model. They must never leak into the shared
# global defaults, or every other model would inherit another model's projector.
MODEL_SCOPED_KEYS = frozenset({"alias", "mmproj", "spec_draft_model", "spec_type"})


def global_defaults(store) -> dict[str, Any]:
    merged = flagmod.defaults()
    saved = store.get("global_flags") or {}
    merged.update({k: v for k, v in saved.items() if k not in MODEL_SCOPED_KEYS})
    return merged


def global_extra(store) -> str:
    return store.get("extra_args") or ""


def detect(repo: scanner.ModelRepo, quant: scanner.GgufFile) -> dict[str, Any]:
    """Wire up sidecar files that sit beside the chosen quant."""
    found: dict[str, Any] = {}
    mmproj = repo.best_companion(scanner.ROLE_MMPROJ, quant)
    if mmproj:
        found["mmproj"] = str(mmproj.path)
    mtp = repo.best_companion(scanner.ROLE_MTP, quant)
    draft = repo.best_companion(scanner.ROLE_DRAFT, quant)
    if mtp:
        found["spec_draft_model"] = str(mtp.path)
        found["spec_type"] = "draft-mtp"
    elif draft:
        found["spec_draft_model"] = str(draft.path)
        found["spec_type"] = "draft-simple"
    return found


def base_values(store, repo: scanner.ModelRepo, quant: scanner.GgufFile) -> dict[str, Any]:
    """What a model gets with no overrides: global defaults, its own alias, detected sidecars."""
    values = global_defaults(store)
    values["alias"] = repo.name
    values.update(detect(repo, quant))
    return values


def overrides(store, repo: scanner.ModelRepo) -> dict[str, Any]:
    profile = store.model_profile(repo.key)
    if "overrides" in profile:
        return dict(profile["overrides"])
    # Not yet migrated: the old full snapshot behaves exactly as it always did.
    return dict(profile.get("flags") or {})


def diff(values: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """The entries of `values` that differ from `base` - i.e. what must be stored as overrides."""
    out = {}
    for key, value in values.items():
        fallback = flagmod.SPECS_BY_KEY[key].default if key in flagmod.SPECS_BY_KEY else None
        if base.get(key, fallback) != value:
            out[key] = value
    return out


def resolve(store, repo: scanner.ModelRepo, quant: scanner.GgufFile) -> tuple[dict[str, Any], str]:
    """Effective (flag values, extra args) for a model, in precedence order."""
    profile = store.model_profile(repo.key)
    values = base_values(store, repo, quant)
    values.update(overrides(store, repo))
    extra = profile["extra_args"] if "extra_args" in profile else global_extra(store)
    return values, extra


def migrate_legacy(store, repos: list[scanner.ModelRepo]) -> list[str]:
    """Turn old full-snapshot profiles into overrides-only ones. Returns migrated repo keys.

    A snapshot value equal to what the model inherits today is dropped, so the model
    starts following the global default for it; anything that differs is kept as a real
    customisation.
    """
    migrated = []
    for repo in repos:
        profile = store.model_profile(repo.key)
        if "overrides" in profile or "flags" not in profile or not repo.quants:
            continue
        quant = next((q for q in repo.quants if str(q.path) == profile.get("quant")), repo.quants[0])
        new = {"quant": profile.get("quant", ""),
               "overrides": diff(profile.get("flags") or {}, base_values(store, repo, quant))}
        extra = profile.get("extra_args")
        if extra is not None and extra != global_extra(store):
            new["extra_args"] = extra
        store.save_model_profile(repo.key, new)
        migrated.append(repo.key)
    return migrated
