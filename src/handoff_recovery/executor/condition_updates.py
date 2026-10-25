"""Optional recovery metadata around an otherwise unchanged native completion."""
import hashlib
import json
import re


PROTOCOL = "native_condition_metadata_v1"
FORMAT_EXTENSION = (
    "\n\n# Recovery condition metadata v1\n"
    "During this recovery EXECUTE step only, the following extends the preceding output-format rules: "
    "keep exactly one normal native action or native completion, with its original tool arguments. "
    "After the native output, you may append one separate <condition_updates>JSON_ARRAY</condition_updates> "
    "block on a new line. For structured tool_calls, put this block in assistant content, not in tool arguments. "
    "Use objects with condition_id, status=\"satisfied\", and concise evidence from the current screenshot. "
    "Do not predict the outcome of the action you are about to perform. Use [] or omit the block if no "
    "condition is newly satisfied. The block is metadata, never another tool call. "
    "Example: <condition_updates>[{\"condition_id\":\"c1\",\"status\":\"satisfied\","
    "\"evidence\":\"The requested saved value is visible\"}]</condition_updates>"
)
FORMAT_SHA256 = hashlib.sha256(FORMAT_EXTENSION.encode()).hexdigest()


def experiment_metadata(mode, agent, completion_policy):
    if agent == "mai_gym":
        if mode == "enabled":
            raise ValueError("MAIGym does not support condition updates")
        return {"condition_updates": "disabled", "condition_updates_protocol": "unsupported",
                "condition_updates_format_sha256": None}
    if agent in {"ui_venus", "openmobile"}:
        if mode == "enabled":
            raise ValueError("UI-Venus does not support condition updates" if agent == "ui_venus"
                             else "OpenMobile does not support condition updates")
        return {"condition_updates": "disabled", "condition_updates_protocol": "unsupported",
                "condition_updates_format_sha256": None}
    native = agent in {"gui_owl", "phonebuddy", "ui_voyager", "holo"}
    effective = mode or ("disabled" if native or completion_policy == "native" else "enabled")
    return {"condition_updates": effective,
            "condition_updates_protocol": (PROTOCOL if native else "json_field_v1"),
            "condition_updates_format_sha256": FORMAT_SHA256 if native and effective == "enabled" else None}


def enabled(instruction):
    return instruction.scope == "execution" and instruction.condition_updates_enabled is True


def request_options(instruction):
    return {"response_format_extension": FORMAT_EXTENSION} if enabled(instruction) else {}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate metadata key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("Nonfinite metadata value")


def parse_native_response(raw, parse_native, *, active):
    """Strip only a framed suffix whose prefix independently passes native parsing.

    Tags inside native strings/reasoning cannot create an action: a candidate
    prefix must itself be a complete valid native response. Ambiguous frames
    are invalid decisions, including when native tool_calls permit free text.
    """
    if active and isinstance(raw, str):
        for marker in re.finditer(r"(?m)^<condition_updates>", raw):
            native = raw[:marker.start()]
            try:
                parsed = parse_native(native)
            except (ValueError, OverflowError):
                continue
            payload = raw[marker.end():].lstrip()
            closing = r"\s*</condition_updates>\s*(?:(?:<\|im_end\|>|<\|endoftext\|>)\s*)*"
            try:
                if len(payload) > 16384:
                    raise ValueError("Oversized condition metadata")
                updates, end = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_nonfinite).raw_decode(payload)
            except (ValueError, TypeError, RecursionError):
                # Malformed JSON can be ignored only within one unambiguous
                # closed block. Never absorb extra native output after it.
                if (payload.count("</condition_updates>") != 1
                        or re.fullmatch(r".*?" + closing, payload, re.DOTALL) is None):
                    raise ValueError("Ambiguous or incomplete condition metadata") from None
                updates = {"invalid_condition_metadata": True}
            else:
                if re.fullmatch(closing, payload[end:]) is None:
                    raise ValueError("Unexpected output after condition metadata")
                if not isinstance(updates, list):
                    updates = {"invalid_condition_metadata": True}
            return parsed, native, updates
    return parse_native(raw), raw, None
