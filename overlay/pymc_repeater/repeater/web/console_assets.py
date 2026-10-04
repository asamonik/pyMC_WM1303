"""Adapt the upstream compiled Console to this backend during overlay deployment.

The Console's Vue sources are not shipped in the WM1303 repository. Keep
changes limited to presentation: peer keys remain identifiers, policy editing
uses the live backend, and missing retry measurements stay unavailable. Recognize every replacement
before publishing a module so an upstream change cannot leave a partial patch.
"""

import os
from pathlib import Path
import stat
import sys
import tempfile


NEIGHBOR_REPLACEMENTS = (
    ("s(e.peer_hash)", "s(e.friendly_name||e.peer_hash)"),
    ("s(G.value.peer_hash)", "s(G.value.friendly_name||G.value.peer_hash)"),
    ("peer_hash:e.peer_hash,path_hash_size:e.path_hash_size",
     "peer_hash:e.peer_hash,friendly_name:e.friendly_name,path_hash_size:e.path_hash_size"),
    ("Peer: ${t.peer_hash}", "Peer: ${t.friendly_name||t.peer_hash}"),
    ("${t.peer_hash} ${t.path_hash_size}", "${t.friendly_name||``} ${t.peer_hash} ${t.path_hash_size}"),
    ("case`peer_hash`:return e.peer_hash;", "case`peer_hash`:return e.friendly_name||e.peer_hash;"),
    ("Search peer hash", "Search node name or peer hash"),
)

POLICY_REPLACEMENTS = (
    ("disabled:!0,onClick:$},` Edit Settings `",
     "disabled:s.value,onClick:$},` Edit Settings `"),
    (" Packet policy enforcement is unavailable in this WM1303 build. "
     "Configure radio forwarding in the Manager's Bridge tab. ",
     " Policy and object management with chained rule conditions "),
)

LBT_REPLACEMENTS = (
    ("c(Y.value?.max_attempts??0)", "c(Y.value?.has_lbt_data?Y.value.max_attempts:`N/A`)"),
    ((" No LBT transmission-path data is available for this window. This is different from zero retries. ",
      " No per-packet LBT retry data is available for this window. "
      "WM1303 bridge records do not store retry attempts. "
      "Channel LBT readings are available in the Manager's Spectrum tab. "),
     " No measured CAD/LBT TX checks are available for this window. "
     "Missing check results do not imply zero retries. "),
)


def adapt_module(source, replacements):
    for before, after in replacements:
        # Some 'before' expressions occur inside the patched text (search).
        if after in source:
            continue
        candidates = (before,) if isinstance(before, str) else before
        matches = [candidate for candidate in candidates if candidate in source]
        if not matches:
            raise ValueError(f"Unrecognized Console module; missing expression: {before}")
        for candidate in matches:
            source = source.replace(candidate, after)
    return source


def patch_console_assets(html_dir):
    assets = Path(html_dir) / "assets"
    pending = []
    for pattern, replacements in (
        ("NeighbourLinks-*.js", NEIGHBOR_REPLACEMENTS),
        ("Configuration-*.js", POLICY_REPLACEMENTS),
        ("RfHealthCorrelation-*.js", LBT_REPLACEMENTS),
    ):
        for module in sorted(assets.glob(pattern)):
            source = module.read_text(encoding="utf-8")
            updated = adapt_module(source, replacements)
            if updated != source:
                pending.append((module, updated))
    # Validate every module first. Keep the existing ownership/mode when the
    # installer runs as root and atomically replace the complete module.
    for module, updated in pending:
        previous = module.stat()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{module.name}.", dir=module.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                current = os.fstat(stream.fileno())
                if (current.st_uid, current.st_gid) != (previous.st_uid, previous.st_gid):
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
                stream.write(updated)
            os.replace(temporary, module)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return len(pending)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: console_assets.py <Console html directory>")
    print(f"Adapted {patch_console_assets(sys.argv[1])} Console modules")
