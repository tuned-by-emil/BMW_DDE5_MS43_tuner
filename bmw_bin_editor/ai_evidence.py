"""Bounded captured-evidence tools and locally validated, reviewable cell plans.

Tools never access paths, execute host code, use the network, or modify a BIN.
The editor separately revalidates a staged plan before one atomic undoable edit.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re

from .core import EditorError, equation_affine, raw_limits
from .ai_setup import setup_facts, calculation_override

MAX_RESULT_BYTES = 220_000
MAX_TABLES_PER_PAGE = 8
MAX_ROWS_PER_PAGE = 500
MAX_PATCH_CELLS = 256
GOALS = {"gear_torque", "launch_torque", "spool", "economy", "smoothing", "other"}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + " must be a finite number.")
    return float(value)


def _integer(value, label, minimum=0, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _text(value, label, maximum=2000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{label} must be {'nonempty ' if required else ''}text, at most {maximum} characters.")
    return value.strip()


def _address(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,12}", value.strip()):
        return int(value.strip(), 16)
    raise ValueError("table_address must be an integer or hexadecimal address such as 0xF7F44.")


def _function(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}


def _nullable(kind, description):
    return {"type": [kind, "null"], "description": description}


_CELL = {"type": "object", "additionalProperties": False, "properties": {
    "table_address": {"type": "string", "description": "Captured hexadecimal base address, e.g. 0xF7F44."},
    "table_id": _nullable("string", "Exact returned table_id if needed to disambiguate aliases; otherwise null."),
    "row": {"type": "integer", "minimum": 0}, "col": {"type": "integer", "minimum": 0},
    "old": {"type": "number", "description": "Exact captured engineering value."},
    "new": {"type": "number", "description": "Proposed engineering value; encoded range is checked locally."},
    "reason": {"type": "string", "description": "Specific cell/evidence reason."},
    "expected_effect": {"type": "string", "description": "Expected direction and uncertainty, not a promised performance gain."}},
    "required": ["table_address", "table_id", "row", "col", "old", "new", "reason", "expected_effect"]}


def evidence_tools():
    return [{"type": "namespace", "name": "editor", "description": "Read captured BIN/log evidence and stage exact cells for user review; never applies or saves edits.", "tools": [
        _function("inspect_tables", "Search ALL captured table definitions, retrieve exact axes/cells/comparison evidence and paginate. Inspect each table before proposing its cells.", {
            "query": {"type": "string", "description": "Title/description/units/address words, empty for all tables."},
            "table_ids": {"type": "array", "items": {"type": "string"}, "description": "Exact returned table IDs or hex addresses, empty to search."},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TABLES_PER_PAGE},
            "include_values": {"type": "boolean", "description": "True for decoded values/axes; false for the index."}}),
        _function("inspect_log", "Retrieve exact sparse observations in a time interval. Paginate; no forward fill or interpolation. Missing channels do not mean zero.", {
            "start_s": _nullable("number", "Inclusive start or null."), "end_s": _nullable("number", "Inclusive end or null."),
            "columns": {"type": "array", "items": {"type": "string"}, "description": "Exact channel names or unambiguous labels, empty for all. time_s always included."},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS_PER_PAGE}}),
        _function("calculate_run", "Calculate fuel-energy power scenarios, coherent airflow/lambda, shifts and estimated economy from ALL selected-run observations. For launch use launch_index or explicit bounds, never mislabel whole-log peak as launch. Wheel speed does not establish grip.", {
            "start_s": _nullable("number", "Inclusive start or null."), "end_s": _nullable("number", "Inclusive end or null."),
            "launch_index": _nullable("integer", "Zero-based chronological loaded-standstill launch, or null for interval/whole capture."),
            "setup_override": _nullable("string", "Optional declared hardware scenario/latest user correction. Use only facts stated by the user/current setup; never invent tyres, gearbox or differential. Affects arithmetic only, never trusted gear bindings or patch authorization. Null uses existing setup.")}),
        _function("propose_patch", "Stage exact old/new cells for explicit editor review, without editing. Inspect tables first. Physical gear targets require trusted captured confirmed_gear_bindings, never assume selector 1 means gear 1. Smoothing requires an explicit smoothing request; curvature alone is not a performance fault. Protective bypasses blocked.", {
            "goal": {"type": "string", "enum": sorted(GOALS)}, "label": {"type": "string", "description": "Short review/undo label."},
            "changes": {"type": "array", "items": _CELL, "minItems": 1, "maxItems": MAX_PATCH_CELLS}})]}]


class EvidenceSession:
    def __init__(self, snapshot, setup="", question=""):
        if not isinstance(snapshot, dict) or _size(snapshot) > 70_000_000:
            raise ValueError("Captured evidence must be an object within the local 70 MB limit.")
        self.snapshot = copy.deepcopy(snapshot)
        self.setup = _text(setup, "setup", 12_000)
        self.question = _text(question, "question", 6000)
        self.proposals, self._inspected = [], set()
        self._tables = self.snapshot.get("tables", [])
        if not isinstance(self._tables, list):
            raise ValueError("Captured tables must be an array.")
        if any(not isinstance(t,dict) or not isinstance(t.get("definition"),dict) for t in self._tables):
            raise ValueError("Each captured table needs an object definition.")
        self._ids = {}
        for i, table in enumerate(self._tables):
            d = table.get("definition", {}) if isinstance(table, dict) else {}
            if type(d.get("z_address")) is int and d["z_address"] >= 0:
                self._ids[f"0x{d['z_address']:X}:{i}"] = (i, table)
        self._log = self.snapshot.get("log")
        if self._log is not None and not isinstance(self._log,dict):
            raise ValueError("Captured log must be an object.")
        if self._log and self.snapshot.get("_captured_log_rows") is not None:
            self._log = dict(self._log, rows=self.snapshot["_captured_log_rows"], complete_rows=True,
                             row_encoding="Exact unsampled captured sparse observations")

    @property
    def tools(self):
        return evidence_tools()

    def call(self, name, args):
        try:
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ValueError("Tool name and argument object are required.")
            handlers = {k: getattr(self, k) for k in ("inspect_tables", "inspect_log", "calculate_run", "propose_patch")}
            name = name.removeprefix("editor.")
            if name not in handlers:
                raise ValueError("Unknown editor evidence tool.")
            if _size(args) > 100_000:
                raise ValueError("Tool arguments exceed the local size limit.")
            result = handlers[name](**args)
            if _size(result) > MAX_RESULT_BYTES:
                raise ValueError("Result exceeds response budget; request fewer tables, rows or columns.")
            return result
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError, EditorError) as exc:
            return {"ok": False, "error": str(exc)[:350] or "Invalid evidence request."}

    def _gear_binding(self, table):
        """Only root/UI-captured confirmation may supersede old XDF uncertainty."""
        d = table.get("definition", {})
        address = d.get("z_address")
        confirmed = self.snapshot.get("confirmed_gear_bindings", {})
        if isinstance(confirmed, dict):
            matches = []
            for key, binding in confirmed.items():
                if not isinstance(binding, dict) or not re.fullmatch(r"[1-8]", str(key)):
                    continue
                try:
                    bound_address = _address(binding.get("table_address"))
                except ValueError:
                    continue
                source = binding.get("source")
                if bound_address == address and isinstance(source, str) and source.strip():
                    matches.append({"physical_gear": int(key), "source": source[:500], "table_address": address})
            if len(matches) == 1:
                return matches[0]
        text = " ".join(str(d.get(k, "")) for k in ("title", "description", "category")).lower()
        if re.search(r"unverified|unresolved|unknown|not proven|not verified|not established|uncertain|selector identity", text):
            return None
        match = re.search(r"verified physical gear\s*[:=]?\s*([1-8])\b", text)
        if match:
            return {"physical_gear": int(match[1]), "source": "Captured verified XDF definition", "table_address": address}
        return None

    @staticmethod
    def _protected(text):
        return bool(re.search(r"protect(?:ive|ion)|overboost|overspeed|(?:temperature|thermal|egt).{0,24}(?:limit|derat|cut)|(?:rail|fuel).{0,15}pressure.{0,15}(?:limit|maximum|cut)|(?:sensor|map).{0,10}(?:linearization|scaling)|\bdtc\b|diagnostic|command bound|command clamp|cutoff|cut-off", text))

    def _identity(self, i, table):
        d = table.get("definition", {})
        address = d.get("z_address")
        text = " ".join(str(d.get(k, "")) for k in ("title", "description", "category")).lower()
        constraints = []
        binding = self._gear_binding(table)
        if ("selector" in text or ("gear" in text and "torque" in text)) and not binding:
            constraints.append("Physical gear assignment unverified; selector number is not physical gear evidence.")
        if self._protected(text):
            constraints.append("Protected function: AI cell proposals unavailable.")
        return {"table_id": f"0x{address:X}:{i}" if type(address) is int else f"unavailable:{i}", "table_index": i,
                "table_address": f"0x{address:X}" if type(address) is int else None,
                "title": d.get("title", ""), "description": d.get("description", ""), "rows": d.get("rows"), "cols": d.get("cols"),
                "units": d.get("z_units", ""), "unavailable": table.get("unavailable"), "gear_binding": binding,
                "changed_vs_comparison_cells": len(table.get("primary_vs_comparison_cells", [])), "patch_constraints": constraints}

    def inspect_tables(self, query="", table_ids=None, offset=0, limit=4, include_values=True):
        query, offset, limit = _text(query, "query", 500), _integer(offset, "offset"), _integer(limit, "limit", 1, MAX_TABLES_PER_PAGE)
        if type(include_values) is not bool:
            raise ValueError("include_values must be boolean.")
        requested = [] if table_ids is None else table_ids
        if not isinstance(requested, list) or len(requested) > 32 or any(not isinstance(x, str) or len(x) > 100 for x in requested):
            raise ValueError("table_ids must contain at most 32 table IDs/hex addresses.")
        addresses = set()
        for item in requested:
            if ":" not in item:
                addresses.add(_address(item))
        tokens = [x.lower() for x in re.findall(r"[\w.]+", query)]
        matches = []
        for i, table in enumerate(self._tables):
            identity = self._identity(i, table)
            if requested and identity["table_id"] not in requested and table.get("definition", {}).get("z_address") not in addresses:
                continue
            text = " ".join(str(identity.get(k, "")) for k in ("title", "description", "units", "table_address")).lower()
            if all(x in text for x in tokens):
                matches.append((i, table, identity))
        result = {"ok": True, "captured_tables": len(self._tables), "matched_tables": len(matches), "offset": offset, "tables": [], "next_offset": None,
                  "evidence_basis": "Captured review; later file edits are not included."}
        for i, table, identity in matches[offset:offset+limit]:
            entry = dict(identity)
            if include_values:
                entry.update({k: copy.deepcopy(table[k]) for k in ("definition", "values", "x_values", "y_values", "original_x_values", "original_y_values", "changes_from_original", "comparison_values", "comparison_x_values", "comparison_y_values", "primary_vs_comparison_cells", "comparison_unavailable", "smoothness_evidence") if k in table})
            if _size(entry) > MAX_RESULT_BYTES-1000:
                entry = dict(identity, values_omitted="Table exceeds one tool result. No cells were silently sampled.")
            if result["tables"] and _size(result)+_size(entry) > MAX_RESULT_BYTES-1000:
                break
            result["tables"].append(entry)
            if "values" in entry:
                self._inspected.add(identity["table_id"])
        next_offset = offset+len(result["tables"])
        if next_offset < len(matches):
            result["next_offset"] = next_offset
        result["returned_tables"] = len(result["tables"])
        return result

    @staticmethod
    def _bounds(start_s, end_s):
        start_s = _number(start_s, "start_s") if start_s is not None else None
        end_s = _number(end_s, "end_s") if end_s is not None else None
        if start_s is not None and end_s is not None and start_s > end_s:
            raise ValueError("start_s must not exceed end_s.")
        return start_s, end_s

    def inspect_log(self, start_s=None, end_s=None, columns=None, offset=0, limit=200):
        if not isinstance(self._log, dict):
            raise ValueError("No captured log is available.")
        start_s, end_s = self._bounds(start_s, end_s)
        offset, limit = _integer(offset, "offset"), _integer(limit, "limit", 1, MAX_ROWS_PER_PAGE)
        source = self._log.get("columns", [])
        if not isinstance(source, list) or not source or source[0] != "time_s":
            raise ValueError("Captured log columns are invalid.")
        columns = [] if columns is None else columns
        if not isinstance(columns, list) or len(columns)>256 or any(not isinstance(x, str) for x in columns):
            raise ValueError("columns must contain at most 256 channel names.")
        channels, indexes = self._log.get("channels", []), []
        for name in columns or source[1:]:
            if name == "time_s":
                continue
            if name in source:
                index = source.index(name)
            else:
                aliases = [c.get("name") for c in channels if c.get("label")==name and c.get("name") in source]
                if len(aliases)!=1:
                    raise ValueError("Unknown or ambiguous channel: "+name[:100])
                index = source.index(aliases[0])
            if index not in indexes:
                indexes.append(index)
        returned_columns = ["time_s"]+[source[i] for i in indexes]
        rows, matched, used = [], 0, 0
        for row in self._log.get("rows", []):
            if not isinstance(row, list) or not row or isinstance(row[0], bool) or not isinstance(row[0], (int,float)) or not math.isfinite(row[0]):
                continue
            if (start_s is not None and row[0]<start_s) or (end_s is not None and row[0]>end_s):
                continue
            if offset <= matched < offset+limit:
                sparse = len(row)==2 and isinstance(row[1], dict)
                observed = {}
                for j, index in enumerate(indexes, 1):
                    v = row[1].get(str(index), row[1].get(index)) if sparse else row[index] if index<len(row) else None
                    if not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v):
                        observed[str(j)] = v
                candidate = [row[0], observed]
                length = _size(candidate)+1
                if used+length < MAX_RESULT_BYTES-20_000:
                    rows.append(candidate); used += length
                else:
                    limit = len(rows)
            matched += 1
        next_offset = offset+len(rows)
        return {"ok": True, "name": self._log.get("name"), "columns": returned_columns, "rows": rows,
                "channels": [c for c in channels if c.get("name") in returned_columns], "matched_rows": matched, "returned_rows": len(rows), "offset": offset,
                "next_offset": next_offset if next_offset<matched else None, "captured_complete_rows": bool(self._log.get("complete_rows")),
                "row_encoding": "[time_s,{column_index:observed_value}], indexes refer to returned columns; missing values are unobserved, never zero or forward filled.",
                "start_s": rows[0][0] if rows else None, "end_s": rows[-1][0] if rows else None}

    @staticmethod
    def _setup_facts(text):
        return setup_facts(text)

    def _calculation_setup(self, setup_override):
        if setup_override is None:
            return self.setup
        override = _text(setup_override, "setup_override", 6000)
        if not override:
            return self.setup
        # Arithmetic selects current facts or this user's explicit scenario.
        # It never changes self.setup, the snapshot or trusted patch bindings.
        return calculation_override(self.setup, self.question, override)

    def calculate_run(self, start_s=None, end_s=None, launch_index=None, setup_override=None):
        if not isinstance(self._log, dict):
            raise ValueError("No captured log is available.")
        start_s, end_s = self._bounds(start_s, end_s)
        if launch_index is not None:
            launch_index = _integer(launch_index, "launch_index", 0, 1000)
        calculation_setup = self._calculation_setup(setup_override)
        from .ai_log_analysis import calculate_run
        result = calculate_run(self._log, calculation_setup, start_s=start_s, end_s=end_s, launch_index=launch_index)
        return {"ok": True, "analysis": result, "captured_complete_rows": bool(self._log.get("complete_rows")),
                "setup_override_used": bool(setup_override),
                "basis": "Captured observations and stated efficiency/scaling assumptions, not dyno power or confirmed grip."}

    def _resolve(self, change):
        address, table_id = _address(change.get("table_address")), change.get("table_id")
        if table_id is not None:
            if table_id not in self._ids or self._ids[table_id][1].get("definition", {}).get("z_address")!=address:
                raise ValueError("table_id does not match the captured address.")
            return self._ids[table_id]
        matches = [(i,t) for i,t in enumerate(self._tables) if t.get("definition",{}).get("z_address")==address]
        if len(matches)!=1:
            raise ValueError("Table address absent or ambiguous; inspect and use its exact table_id.")
        return matches[0]

    def _validate_plan(self, goal, label, changes, require_inspection=True):
        if goal not in GOALS:
            raise ValueError("Unknown patch goal.")
        label = _text(label, "label", 160, True)
        if not isinstance(changes,list) or not 1<=len(changes)<=MAX_PATCH_CELLS:
            raise ValueError(f"A patch requires 1 to {MAX_PATCH_CELLS} exact cells.")
        smoothing_requested = bool(re.search(r"\bsmooth\w*\b|\bblend\w*\b|\binterpolat\w*\b",self.question,re.I))
        if goal=="smoothing" and not smoothing_requested:
            raise ValueError("Smoothing was not requested. Numerical curvature alone does not justify a launch, spool or economy patch.")
        method_text = " ".join([label]+[str(c.get("reason","")) for c in changes if isinstance(c,dict)]).lower()
        if goal not in {"gear_torque","launch_torque"} and not smoothing_requested and re.search(r"\bsmooth\w*\b|\bblend\w*\b|\btrough\b|\bcurvature\b|\binterpolat\w*\b",method_text):
            raise ValueError("Unrequested numerical smoothing cannot be relabeled as a performance fix. Retrieve evidence of the active fault first.")
        text = " ".join([label,self.question]+[str(c.get("reason",""))+" "+str(c.get("expected_effect","")) for c in changes if isinstance(c,dict)]).lower()
        gear_goal = goal in {"gear_torque","launch_torque"} or bool(re.search(r"(?:[1-8](?:st|nd|rd|th)?|first|second|third|fourth|fifth)\s*(?:physical\s+)?gears?\b|\bgears?\s*[1-8]\b|physical gear|gear-specific",text))
        if re.search(r"(?:disable|bypass|delete|remove|defeat).{0,35}(?:protect|cut|limit|diagnostic|dtc|smoke)|(?:protect|diagnostic|dtc).{0,25}(?:disable|bypass|delete)",text):
            raise ValueError("Protection/diagnostic bypass is not an available AI patch operation.")
        normalized, claimed_bytes = [], set()
        for c in changes:
            if not isinstance(c,dict):
                raise ValueError("Every change must be an object.")
            i,t = self._resolve(c)
            identity, d = self._identity(i,t), t.get("definition",{})
            if require_inspection and identity["table_id"] not in self._inspected:
                raise ValueError("Inspect exact table values before proposing cells: "+identity["table_id"])
            description = " ".join(str(d.get(k,"")) for k in ("title","description","category")).lower()
            binding = self._gear_binding(t)
            if self._protected(description):
                raise ValueError("AI patching of this protective function is unavailable: "+identity["title"][:100])
            selector_table = "selector" in description or ("gear" in description and "torque" in description)
            gear_identity = " ".join(str(d.get(k,"")) for k in ("title","category","categories")).lower()
            gear_torque_table = "gear" in gear_identity and ("torque" in gear_identity or "ceiling" in gear_identity)
            if (gear_torque_table or (gear_goal and selector_table) or goal=="gear_torque") and not binding:
                raise ValueError("Physical gear-to-selector mapping unverified. Selector 1 is not evidence of first gear; no gear patch staged.")
            uncertain_function = bool(re.search(r"^unverified (?:pattern candidate|semantic hypothesis)|classification\s*:\s*unverified|function.{0,15}(?:is|remains) unverified|engineering scal(?:e|ing).{0,15}unverified", str(d.get("description", "")).lower()))
            if uncertain_function and not (selector_table and binding):
                raise ValueError("Table function is unverified; functional patch requires verified definition evidence.")
            cell_text = (str(c.get("reason", ""))+" "+str(c.get("expected_effect", ""))).lower()
            if binding:
                claimed_gears = {int(value) for match in re.findall(r"\bgear\s*([1-8])\b|\b([1-8])(?:st|nd|rd|th)\s+gear\b", cell_text) for value in match if value}
                for word, number in (("first",1),("second",2),("third",3),("fourth",4),("fifth",5)):
                    if re.search(r"\b"+word+r"\s+gear\b",cell_text):
                        claimed_gears.add(number)
                if len(claimed_gears)==1 and binding["physical_gear"] not in claimed_gears:
                    raise ValueError("Cell's claimed physical gear conflicts with the trusted captured selector binding.")
            values = t.get("values")
            if t.get("unavailable") or not isinstance(values,list):
                raise ValueError("Table values unavailable; no patch staged.")
            row,col = _integer(c.get("row"),"row",0,100_000),_integer(c.get("col"),"col",0,100_000)
            rows,cols = _integer(d.get("rows"),"table rows",1,100_000),_integer(d.get("cols"),"table cols",1,100_000)
            if row>=rows or col>=cols or row>=len(values) or col>=len(values[row]):
                raise ValueError("Cell outside captured dimensions.")
            actual,old = _number(values[row][col],"captured old"),_number(c.get("old"),"old")
            displayed_new = _number(c.get("new"),"new")
            requested_new = _number(c.get("requested_new",displayed_new),"requested_new")
            if not math.isclose(actual,old,rel_tol=1e-10,abs_tol=1e-8):
                raise ValueError(f"Old value mismatch [{row},{col}]: expected {actual:g}.")
            scale,offset = equation_affine(_text(d.get("z_equation","X"),"captured scale",160))
            bits,signed = d.get("z_element_bits",16),d.get("z_signed",False)
            if type(bits) is not int or type(signed) is not bool:
                raise ValueError("Captured element type invalid.")
            low,high = raw_limits(bits,signed)
            rf = (requested_new-offset)/scale
            if not math.isfinite(rf) or rf<low-1e-9 or rf>high+1e-9:
                raise ValueError("New value outside captured raw storage range.")
            raw = int(round(rf)); new = raw*scale+offset
            if "requested_new" in c and not math.isclose(displayed_new,new,rel_tol=1e-12,abs_tol=max(abs(scale)*1e-6,1e-10)):
                raise ValueError("Displayed replacement differs from its encoded requested value. Review a consistent fresh proposal.")
            if not math.isfinite(new) or math.isclose(new,actual,rel_tol=1e-12,abs_tol=max(abs(scale)*1e-8,1e-10)):
                raise ValueError("Change is unrepresentable or a no-op at stored resolution.")
            if "smoke" in description and "limit" in description and new>actual:
                raise ValueError("Raising an unvalidated smoke/fuel protection limit is unavailable.")
            order = d.get("storage_order","row_major")
            if order not in {"row_major","column_major"}:
                raise ValueError("Captured storage order unsupported.")
            address = d["z_address"]+(col*rows+row if order=="column_major" else row*cols+col)*(bits//8)
            size = (self.snapshot.get("bin") or {}).get("byte_length")
            if address<0 or (type(size) is int and address+bits//8>size):
                raise ValueError("Cell address outside captured BIN.")
            cellbytes = set(range(address,address+bits//8))
            if cellbytes & claimed_bytes:
                raise ValueError("Duplicate/overlapping cell writes; use one unambiguous change per byte.")
            claimed_bytes.update(cellbytes)
            normalized.append({"table_id":identity["table_id"],"table_index":i,"table_address":d["z_address"],"table_title":identity["title"],
                "definition_sha256":_digest(d),"row":row,"col":col,"old":actual,"new":new,"requested_new":requested_new,"raw":raw,"byte_address":address,
                "x":(t.get("x_values") or [])[col] if col<len(t.get("x_values") or []) else None,
                "y":(t.get("y_values") or [])[row] if row<len(t.get("y_values") or []) else None,
                "reason":_text(c.get("reason"),"reason",1000,True),"expected_effect":_text(c.get("expected_effect"),"expected_effect",1000,True),
                "gear_binding":binding})
        signature = self.snapshot.get("calibration_signature")
        if not isinstance(signature,dict) or not signature.get("bin") or not signature.get("definition"):
            raise ValueError("Captured BIN and definition signatures required for editable cells.")
        plan = {"label":label,"goal":goal,"calibration_signature":copy.deepcopy(signature),"request_question":self.question,"setup":self.setup,
            "changes":normalized,"cell_count":len(normalized),"status":"staged","applied":False,
            "evidence_status":"Exact captured cells/storage validated; active state and performance effect are not established by this plan."}
        effects = list(dict.fromkeys(c["expected_effect"] for c in normalized))
        plan["expected_effect"] = " ".join(effects)[:2000]
        plan["id"] = _digest({"signature":signature,"goal":goal,"label":label,"cells":[[c["table_address"],c["row"],c["col"],c["old"],c["new"]] for c in normalized]})[:16]
        return plan

    def propose_patch(self, goal, label, changes):
        if len(self.proposals)>=6:
            raise ValueError("Six proposals already staged; review an existing plan first.")
        plan = self._validate_plan(goal,label,changes)
        duplicate = any(p["id"]==plan["id"] for p in self.proposals)
        if not duplicate:
            self.proposals.append(plan)
        return {"ok":True,"proposal":copy.deepcopy(plan),"already_staged":duplicate,"message":"Staged for explicit editor review. No BIN/files changed."}


def validate_proposal(snapshot, proposal):
    """Recompute every saved/model plan field; claimed 'validated' flags are ignored."""
    if not isinstance(proposal,dict):
        raise ValueError("Patch proposal must be an object.")
    if proposal.get("calibration_signature")!=snapshot.get("calibration_signature"):
        raise ValueError("BIN/definition changed after proposal. Capture and review a fresh plan.")
    try:
        session = EvidenceSession(snapshot,setup=proposal.get("setup",""),question=proposal.get("request_question",""))
        return session._validate_plan(proposal.get("goal"),proposal.get("label"),proposal.get("changes"),require_inspection=False)
    except (TypeError,KeyError,IndexError,AttributeError,ArithmeticError,EditorError) as exc:
        raise ValueError(str(exc)[:350] or "Malformed patch proposal.") from None
