"""
MO2 Mod Bisect Tool v4
Find FPS-killing plugins via subtractive binary search.
Reads your full load order — no suspects file needed.
Syncs both plugins.txt (right pane) and modlist.txt (left pane).

Subtractive approach: disables one half at a time (keeps most plugins on).
Fewer master issues, works for cumulative FPS problems.
"""

import os
import json
import struct
import glob
import shutil
from datetime import datetime

import mobase
from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QIcon, QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QTextEdit, QMessageBox, QApplication, QFrame,
    QSpinBox, QGroupBox, QFileDialog,
)

# Base game plugins that should never be disabled
BASE_PLUGINS = {
    "fallout4.esm", "dlcrobot.esm", "dlcworkshop01.esm",
    "dlccoast.esm", "dlcworkshop02.esm", "dlcworkshop03.esm",
    "dlcnukaworld.esm",
}

# Mod folder name patterns to never disable (case-insensitive substring match).
# Keep this minimal — only mods the user specifically asked to exclude.
DEFAULT_EXCLUDE_PATTERNS = [
    "address library",
    "high fps",
    "addictol",
    "buffout",
    "classic holstered",
    "unofficial fallout 4 patch",
]


class BisectEngine:
    THRESHOLD = 1

    def __init__(self, profile_dir, mods_dir, overwrite_dir=None, exclude_patterns=None, organizer=None):
        self.profile_dir = profile_dir
        self.mods_dir = mods_dir
        self.overwrite_dir = overwrite_dir
        self.exclude_patterns = exclude_patterns or DEFAULT_EXCLUDE_PATTERNS
        self._organizer = organizer
        self.plugins_file = os.path.join(profile_dir, "plugins.txt")
        self.modlist_file = os.path.join(profile_dir, "modlist.txt")
        self.loadorder_file = os.path.join(profile_dir, "loadorder.txt")
        self.state_file = os.path.join(profile_dir, "bisect_state.json")
        self.plugins_backup = self.plugins_file + ".bisect_backup"
        self.modlist_backup = self.modlist_file + ".bisect_backup"
        self.loadorder_backup = self.loadorder_file + ".bisect_backup"
        self.log_file = os.path.join(profile_dir, "bisect_log.txt")
        self._plugin_to_mod = {}
        self._plugin_paths = {}  # plugin_lower -> file path (fallback only)

    @staticmethod
    def read_masters(plugin_path):
        masters = []
        try:
            with open(plugin_path, 'rb') as f:
                if f.read(4) != b'TES4':
                    return masters
                data_size = struct.unpack('<I', f.read(4))[0]
                f.read(16)
                end_pos = f.tell() + data_size
                while f.tell() < end_pos:
                    sub_type = f.read(4)
                    if len(sub_type) < 4:
                        break
                    sub_size = struct.unpack('<H', f.read(2))[0]
                    sub_data = f.read(sub_size)
                    if sub_type == b'MAST':
                        masters.append(sub_data.rstrip(b'\x00').decode('utf-8', errors='replace'))
        except Exception:
            pass
        return masters

    def get_all_known_plugins(self):
        """Get ALL plugins MO2 knows about, including disabled ones not in plugins.txt."""
        if self._organizer:
            try:
                return list(self._organizer.pluginList().pluginNames())
            except Exception:
                pass
        # Fallback: return what we can find on disk
        return list(self._plugin_paths.keys())

    def get_plugin_masters(self, plugin_name):
        """Get masters for a plugin. Uses MO2 API first, falls back to file reading."""
        if self._organizer:
            try:
                masters = list(self._organizer.pluginList().masters(plugin_name))
                if masters is not None:
                    return masters
            except Exception:
                pass
        # Fallback: read from file
        path = self._plugin_paths.get(plugin_name.lower())
        if path:
            return self.read_masters(path)
        return []

    def build_plugin_to_mod_map(self, all_plugins):
        """Build plugin -> mod mapping. Uses MO2 API first, falls back to folder scan."""
        if self._organizer:
            try:
                plugin_list = self._organizer.pluginList()
                mapping = {}
                for p in all_plugins:
                    origin = plugin_list.origin(p)
                    if origin:
                        mapping[p.lower()] = origin
                if mapping:
                    self._plugin_to_mod = mapping
                    return mapping
            except Exception:
                pass
        # Fallback: scan mod folders
        self._plugin_to_mod = self._scan_mod_folders()
        return self._plugin_to_mod

    def _scan_mod_folders(self):
        """Fallback: scan mod folders + overwrite for plugin files."""
        plugin_to_mod = {}
        for mod_folder in os.listdir(self.mods_dir):
            mod_path = os.path.join(self.mods_dir, mod_folder)
            if not os.path.isdir(mod_path):
                continue
            for ext in ('*.esp', '*.esm', '*.esl'):
                for p in glob.glob(os.path.join(mod_path, ext)):
                    name = os.path.basename(p).lower()
                    if name not in plugin_to_mod:
                        plugin_to_mod[name] = mod_folder
                    if name not in self._plugin_paths:
                        self._plugin_paths[name] = p
        if self.overwrite_dir and os.path.isdir(self.overwrite_dir):
            for ext in ('*.esp', '*.esm', '*.esl'):
                for p in glob.glob(os.path.join(self.overwrite_dir, ext)):
                    name = os.path.basename(p).lower()
                    if name not in self._plugin_paths:
                        self._plugin_paths[name] = p
        return plugin_to_mod

    def read_enabled_plugins(self):
        """Read only ENABLED plugins from plugins.txt (those with * prefix)."""
        plugins = []
        with open(self.plugins_file, "r", encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("*"):
                    plugins.append(line[1:])
                # Skip non-* entries — they're disabled, not part of bisection
        return plugins

    def _is_excluded_mod(self, mod_folder):
        """Check if a mod folder matches any exclude pattern."""
        folder_lower = mod_folder.lower()
        for pattern in self.exclude_patterns:
            if pattern.lower() in folder_lower:
                return True
        return False

    def classify_plugins(self, all_plugins):
        """Split plugins into base (always on) and testable (can be bisected).

        Base = game ESMs + excluded frameworks + all their masters (recursive).
        Testable = everything else.
        """
        all_lower = {p.lower(): p for p in all_plugins}

        # First pass: identify directly excluded plugins
        # Check both mod folder name AND plugin name against exclude patterns
        base_set = set(BASE_PLUGINS)
        excluded_mods = set()
        for p in all_plugins:
            # Check plugin name itself (e.g. "Unofficial Fallout 4 Patch.esp")
            if self._is_excluded_mod(p):
                base_set.add(p.lower())
                mod_folder = self._plugin_to_mod.get(p.lower(), p)
                excluded_mods.add(mod_folder)
            # Check mod folder name
            elif p.lower() in self._plugin_to_mod:
                mod_folder = self._plugin_to_mod[p.lower()]
                if self._is_excluded_mod(mod_folder):
                    base_set.add(p.lower())
                    excluded_mods.add(mod_folder)

        # Recursively exclude masters of excluded plugins (so they stay enabled too)
        changed = True
        while changed:
            changed = False
            for plugin_key in list(base_set):
                masters = self.get_plugin_masters(plugin_key)
                for m in masters:
                    if m.lower() in all_lower and m.lower() not in base_set:
                        base_set.add(m.lower())
                        changed = True

        base = []
        testable = []
        for p in all_plugins:
            if p.lower() in base_set:
                base.append(p)
            else:
                testable.append(p)
        return base, testable, excluded_mods

    def build_dependency_groups(self, testable):
        """Group testable plugins by dependencies. Returns (groups, cascade, deps, all_masters)."""
        testable_lower = {s.lower(): s for s in testable}
        deps = {}         # testable masters only (for grouping)
        all_masters = {}  # ALL masters per plugin (for missing master checks)
        not_found = []
        for s in testable:
            masters = self.get_plugin_masters(s)
            if masters is not None:
                all_masters[s.lower()] = [m.lower() for m in masters]
                suspect_masters = [m.lower() for m in masters if m.lower() in testable_lower]
                if suspect_masters:
                    deps[s.lower()] = suspect_masters
            else:
                not_found.append(s)

        # Cascade: plugins depending on 5+ other testable plugins
        cascade_keys = set()
        for key, masters in deps.items():
            if len(masters) >= 5:
                cascade_keys.add(key)

        filtered = [s for s in testable if s.lower() not in cascade_keys]
        cascade = [s for s in testable if s.lower() in cascade_keys]
        filtered_set = {s.lower() for s in filtered}

        # Union-Find
        parent = {}
        for s in filtered:
            parent[s.lower()] = s.lower()

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for pkey, masters in deps.items():
            if pkey in filtered_set:
                for m in masters:
                    if m in filtered_set:
                        union(pkey, m)

        groups = {}
        for s in filtered:
            root = find(s.lower())
            groups.setdefault(root, []).append(s)

        return list(groups.values()), cascade, deps, all_masters, not_found

    def protect_masters(self, disabled_plugins, all_testable, all_masters, base_set):
        """Prevent disabling plugins that are masters of enabled plugins.

        In subtractive bisection, we disable a subset. But if an enabled plugin
        needs a disabled plugin as a master, we must keep that master enabled
        (remove it from the disabled set).

        Returns (actual_disabled, protected) where protected are plugins that
        were kept enabled to satisfy dependencies.
        """
        testable_lower = {p.lower(): p for p in all_testable}
        disabled = {p.lower() for p in disabled_plugins}
        enabled = {p.lower() for p in all_testable if p.lower() not in disabled}
        protected = set()

        changed = True
        while changed:
            changed = False
            for plugin in list(enabled):
                # Check cached masters first, then live API as fallback
                masters = all_masters.get(plugin, [])
                if not masters:
                    orig_name = testable_lower.get(plugin, plugin)
                    live = self.get_plugin_masters(orig_name)
                    if live:
                        masters = [m.lower() for m in live]
                        all_masters[plugin] = masters
                for master_l in masters:
                    if master_l in base_set:
                        continue  # base plugin, always on
                    if master_l in disabled:
                        # This master is needed — keep it enabled
                        disabled.remove(master_l)
                        enabled.add(master_l)
                        protected.add(master_l)
                        changed = True

        actual_disabled = [testable_lower[p] for p in disabled if p in testable_lower]
        protected_names = [testable_lower[p] for p in protected if p in testable_lower]
        return actual_disabled, protected_names

    def sync_modlist(self, enabled_plugins):
        """Sync modlist.txt left pane to match enabled plugins.

        Mods with at least one enabled plugin get +, others get -.
        Excluded framework mods and mods not in plugin_to_mod are left untouched.
        """
        enabled_lower = {p.lower() for p in enabled_plugins}

        enabled_mods = set()
        for plugin_key, mod_folder in self._plugin_to_mod.items():
            if plugin_key in enabled_lower:
                enabled_mods.add(mod_folder)

        # Only touch mod folders that contain testable (non-excluded) plugins
        testable_mods = set()
        for mod_folder in self._plugin_to_mod.values():
            if not self._is_excluded_mod(mod_folder):
                testable_mods.add(mod_folder)

        with open(self.modlist_file, "r") as f:
            lines = f.readlines()

        new_lines = []
        changed = 0
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                new_lines.append(line)
                continue
            prefix = s[0]
            mod_name = s[1:] if prefix in ('+', '-') else s
            if mod_name in testable_mods:
                if mod_name in enabled_mods:
                    new_line = "+" + mod_name + "\n"
                else:
                    new_line = "-" + mod_name + "\n"
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
            else:
                new_lines.append(line)

        with open(self.modlist_file, "w") as f:
            f.writelines(new_lines)
        return changed

    def order_plugins(self, groups, all_deps):
        """Topological sort so masters load before dependents."""
        enabled = set()
        for g in groups:
            for p in g:
                enabled.add(p.lower())
        in_deg = {p.lower(): 0 for g in groups for p in g}
        fwd = {p.lower(): [] for g in groups for p in g}
        for pkey, masters in all_deps.items():
            if pkey in enabled:
                for m in masters:
                    if m in enabled:
                        in_deg[pkey] = in_deg.get(pkey, 0) + 1
                        fwd.setdefault(m, []).append(pkey)
        queue = [k for k, v in in_deg.items() if v == 0]
        ordered = []
        while queue:
            node = queue.pop(0)
            ordered.append(node)
            for dep in fwd.get(node, []):
                in_deg[dep] -= 1
                if in_deg[dep] == 0:
                    queue.append(dep)
        case_map = {}
        for g in groups:
            for p in g:
                case_map[p.lower()] = p
        return [case_map[k] for k in ordered if k in case_map]

    def backup_files(self):
        for src, dst in [(self.plugins_file, self.plugins_backup),
                         (self.modlist_file, self.modlist_backup),
                         (self.loadorder_file, self.loadorder_backup)]:
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)

    def restore_backups(self):
        # Restore modlist (left pane) FIRST so mods are enabled
        # before their plugins come back in plugins.txt,
        # then plugins.txt, then loadorder.txt to fix right pane order
        for dst, src in [(self.modlist_file, self.modlist_backup),
                         (self.plugins_file, self.plugins_backup),
                         (self.loadorder_file, self.loadorder_backup)]:
            if os.path.exists(src):
                shutil.copy2(src, dst)
                os.remove(src)
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

    def write_plugins(self, base_plugins, enabled_testable):
        """Write plugins.txt preserving original order from backup.

        Keeps all base/excluded plugins, adds enabled testable plugins,
        removes disabled testable plugins. Original load order is preserved.
        Syncs left pane FIRST so mods are enabled before their plugins.
        """
        enabled_set = {p.lower() for p in enabled_testable}
        base_set = {p.lower() for p in base_plugins}

        # Sync left pane FIRST — enable mods before their plugins
        self.sync_modlist(base_plugins + enabled_testable)

        # Read the original backup to preserve order
        source = self.plugins_backup if os.path.exists(self.plugins_backup) else self.plugins_file
        with open(source, "r", encoding='utf-8-sig') as f:
            original_lines = f.readlines()

        with open(self.plugins_file, "w", encoding='utf-8') as f:
            for line in original_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    f.write(line)
                    continue
                # Get plugin name (strip * prefix if present)
                was_enabled = stripped.startswith("*")
                name = stripped[1:] if was_enabled else stripped
                name_lower = name.lower()
                if name_lower in base_set:
                    # Always keep base plugins enabled
                    f.write("*{}\n".format(name))
                elif name_lower in enabled_set:
                    # Testable plugin that's enabled this round
                    f.write("*{}\n".format(name))
                elif was_enabled:
                    # Originally enabled testable plugin, now disabled — keep in file without * prefix
                    f.write("{}\n".format(name))
                else:
                    # Originally disabled — preserve as-is, don't touch it
                    f.write("{}\n".format(name))

    def save_state(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        return None

    def has_state(self):
        return os.path.exists(self.state_file)

    def append_log(self, message):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = "[{}] {}".format(ts, message)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
        return entry

    def read_log(self):
        if os.path.exists(self.log_file):
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        return ""

    def clear_log(self):
        """Archive old log before starting fresh."""
        if os.path.exists(self.log_file):
            # Archive to Desktop with timestamp
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            archive = os.path.join(desktop, "bisect_log_{}.txt".format(ts))
            shutil.copy2(self.log_file, archive)
            # Also append to a persistent history log in the profile folder
            history_file = os.path.join(self.profile_dir, "bisect_history.txt")
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                old_content = f.read()
            with open(history_file, "a", encoding="utf-8") as f:
                f.write("\n" + old_content + "\n")
            os.remove(self.log_file)

    def _auto_save_log(self):
        """Save the current log to Desktop when bisection completes."""
        if not os.path.exists(self.log_file):
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        dest = os.path.join(desktop, "bisect_results_{}.txt".format(ts))
        shutil.copy2(self.log_file, dest)
        self.append_log("Results saved to: {}".format(dest))

    @staticmethod
    def _split_by_plugin_count(indices, groups):
        """Split indices into two halves balanced by plugin count, not group count."""
        if len(indices) <= 1:
            # Can't split a single group — return it as half_a, empty half_b
            return indices, []
        total = sum(len(groups[i]) for i in indices)
        target = total // 2
        running = 0
        split_at = 1  # at least 1 in first half
        for idx, i in enumerate(indices):
            running += len(groups[i])
            if running >= target:
                # Check if splitting before or after this group gives better balance
                if idx > 0:
                    before = running - len(groups[i])
                    after = running
                    if abs(before - target) < abs(after - target):
                        split_at = idx
                    else:
                        split_at = idx + 1
                else:
                    split_at = idx + 1
                break
        # Ensure both halves are non-empty
        split_at = max(1, min(split_at, len(indices) - 1))
        return indices[:split_at], indices[split_at:]

    def _compute_enabled(self, state, disabled_indices):
        """Compute the enabled testable plugins for a subtractive test.

        All testable plugins EXCEPT those in disabled_indices groups are enabled.
        Then protect_masters keeps any disabled plugin that's needed as a master.
        """
        groups = state["groups"]
        all_testable = state.get("all_testable", [])
        all_masters_map = state.get("all_masters", {})
        base = state["base_plugins"]
        base_set = {p.lower() for p in base}

        disabled_set = set()
        for i in disabled_indices:
            for p in groups[i]:
                disabled_set.add(p.lower())

        disabled_plugins = [p for p in all_testable if p.lower() in disabled_set]
        actual_disabled, protected = self.protect_masters(
            disabled_plugins, all_testable, all_masters_map, base_set)

        enabled = [p for p in all_testable if p.lower() not in {d.lower() for d in actual_disabled}]
        return enabled, actual_disabled, protected

    # --- Commands ---

    def setup(self, baseline_fps, all_on_fps):
        """Start subtractive bisection. Disables one half at a time."""
        if self.has_state():
            return None, "Bisection already in progress. Restore first to start over."

        # Read current load order
        all_plugins = self.read_enabled_plugins()

        # Build plugin -> mod mapping (MO2 API preferred, folder scan fallback)
        self.build_plugin_to_mod_map(all_plugins)
        base, testable, excluded_mods = self.classify_plugins(all_plugins)

        # Build dependency groups
        groups, cascade, deps, all_masters, not_found = self.build_dependency_groups(testable)

        self.clear_log()
        self.append_log("=== MO2 Mod Bisect v4 (Subtractive) Started ===")
        self.append_log("Profile: {}".format(os.path.basename(self.profile_dir)))
        self.append_log("Total plugins: {} ({} base/excluded, {} testable, {} cascade)".format(
            len(all_plugins), len(base), len(testable) - len(cascade), len(cascade)))
        self.append_log("Baseline: {} FPS | All on: {} FPS | Cost: {} FPS".format(
            baseline_fps, all_on_fps, baseline_fps - all_on_fps))
        self.append_log("{} dependency groups".format(len(groups)))
        if not_found:
            self.append_log("WARNING: {} plugins not found on disk (can't read masters):".format(len(not_found)))
            for p in not_found[:20]:
                self.append_log("  [?] {}".format(p))
            if len(not_found) > 20:
                self.append_log("  ... and {} more".format(len(not_found) - 20))
        if excluded_mods:
            self.append_log("Excluded frameworks ({} mods):".format(len(excluded_mods)))
            for m in sorted(excluded_mods):
                self.append_log("  [skip] {}".format(m))
        if cascade:
            self.append_log("Cascade (always off): {}".format(", ".join(cascade)))
        multi = [g for g in groups if len(g) > 1]
        for g in multi:
            self.append_log("  Group: {} (+{} deps)".format(g[0], len(g) - 1))

        total_cost = baseline_fps - all_on_fps
        if total_cost <= 5:
            msg = "Only {} FPS difference. Nothing significant to find.".format(total_cost)
            self.append_log(msg)
            return None, msg

        # Backup
        self.backup_files()

        # Start bisecting — split into two halves, DISABLE first half
        all_indices = list(range(len(groups)))
        half_a, half_b = self._split_by_plugin_count(all_indices, groups)

        # Disable bottom half first — bottom has patches/dependents, top has masters.
        # Keeping top enabled means fewer missing master issues.
        work_queue = [
            {"indices": half_a, "label": "A"},
        ]

        first_test = {"indices": half_b, "label": "B"}

        # Remove cascade plugins from testable — they're always off during bisection
        # (they have 5+ testable masters and would shield those masters from being disabled)
        cascade_lower = {c.lower() for c in cascade}
        testable_active = [p for p in testable if p.lower() not in cascade_lower]

        state = {
            "base_plugins": base,
            "all_testable": testable_active,
            "groups": groups,
            "cascade": cascade,
            "all_deps": deps,
            "all_masters": all_masters,
            "plugin_to_mod": self._plugin_to_mod,
            "phase": "testing",
            "baseline_fps": baseline_fps,
            "all_on_fps": all_on_fps,
            "work_queue": work_queue,
            "culprits": [],
            "disabled_indices": half_b,
            "current_test": first_test,
            "round": 0,
            "history": [],
        }

        # Compute enabled plugins (all testable minus disabled half)
        enabled, actual_disabled, protected = self._compute_enabled(state, half_b)
        if protected:
            self.append_log("Protected {} masters from being disabled: {}".format(
                len(protected), ", ".join(protected[:10])))
        self.write_plugins(base, enabled)

        self.save_state(state)

        a_count = sum(len(groups[i]) for i in half_a)
        b_count = sum(len(groups[i]) for i in half_b)
        total_testable = a_count + b_count
        msg = "{} testable plugins in {} groups.\nDisabling B (bottom half, {} plugins off, {} still on).\nIf FPS is GOOD -> culprits are in the disabled group.\nIf FPS is BAD -> disabled group is clean.\nLaunch game and report result.".format(
            total_testable, len(groups), len(actual_disabled), len(enabled))
        self.append_log(msg)
        return state, msg

    def setup_from_list(self, baseline_fps, all_on_fps, suspect_file):
        """Start subtractive bisection using only plugins from a file as testable."""
        if self.has_state():
            return None, "Bisection already in progress. Restore first to start over."

        # Read suspect list
        with open(suspect_file, "r", encoding="utf-8-sig") as f:
            suspect_names = set()
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    suspect_names.add(line.lower())

        all_plugins = self.read_enabled_plugins()
        self.build_plugin_to_mod_map(all_plugins)

        # Initial split: suspects are testable, everything else is base
        # Also move excluded frameworks (UFO4P etc.) to base even if in the import
        base = []
        testable = []
        excluded_from_import = []
        for p in all_plugins:
            if p.lower() in suspect_names:
                if p.lower() in BASE_PLUGINS or self._is_excluded_mod(p):
                    base.append(p)
                    excluded_from_import.append(p)
                else:
                    mod_folder = self._plugin_to_mod.get(p.lower(), "")
                    if mod_folder and self._is_excluded_mod(mod_folder):
                        base.append(p)
                        excluded_from_import.append(p)
                    else:
                        testable.append(p)
            else:
                base.append(p)

        # Safety: move testable plugins to base if any base plugin needs them as master.
        # Otherwise disabling them during bisect breaks the base plugins.
        testable_set = {p.lower() for p in testable}
        needed_by_base = set()
        for bp in base:
            for m in self.get_plugin_masters(bp):
                if m.lower() in testable_set:
                    needed_by_base.add(m.lower())
        # Transitively: if a needed plugin itself needs other testable plugins
        changed = True
        while changed:
            changed = False
            for tp in list(needed_by_base):
                for m in self.get_plugin_masters(tp):
                    if m.lower() in testable_set and m.lower() not in needed_by_base:
                        needed_by_base.add(m.lower())
                        changed = True
        if needed_by_base:
            new_testable = []
            for p in testable:
                if p.lower() in needed_by_base:
                    base.append(p)
                else:
                    new_testable.append(p)
            testable = new_testable

        if not testable:
            return None, "No matching plugins found in suspect file ({} names loaded).".format(len(suspect_names))

        groups, cascade, deps, all_masters, not_found = self.build_dependency_groups(testable)

        self.clear_log()
        self.append_log("=== MO2 Mod Bisect v4 (Subtractive, imported {} suspects) ===".format(len(testable)))
        self.append_log("Profile: {}".format(os.path.basename(self.profile_dir)))
        if excluded_from_import:
            self.append_log("Auto-excluded {} imported plugins (frameworks):".format(len(excluded_from_import)))
            for p in excluded_from_import:
                self.append_log("  [skip] {}".format(p))
        if needed_by_base:
            self.append_log("Moved {} imported plugins to base (required by non-imported plugins):".format(
                len(needed_by_base)))
            for p in sorted(needed_by_base)[:20]:
                self.append_log("  [base] {}".format(p))
            if len(needed_by_base) > 20:
                self.append_log("  ... and {} more".format(len(needed_by_base) - 20))
        self.append_log("Imported from: {}".format(os.path.basename(suspect_file)))
        self.append_log("Total plugins: {} ({} base, {} testable, {} cascade)".format(
            len(all_plugins), len(base), len(testable) - len(cascade), len(cascade)))
        self.append_log("Baseline: {} FPS | All on: {} FPS | Cost: {} FPS".format(
            baseline_fps, all_on_fps, baseline_fps - all_on_fps))
        self.append_log("{} dependency groups".format(len(groups)))
        if cascade:
            self.append_log("Cascade (always off): {}".format(", ".join(cascade)))
        multi = [g for g in groups if len(g) > 1]
        for g in multi:
            self.append_log("  Group: {} (+{} deps)".format(g[0], len(g) - 1))

        total_cost = baseline_fps - all_on_fps
        if total_cost <= 5:
            msg = "Only {} FPS difference. Nothing significant to find.".format(total_cost)
            self.append_log(msg)
            return None, msg

        self.backup_files()

        all_indices = list(range(len(groups)))
        half_a, half_b = self._split_by_plugin_count(all_indices, groups)

        # Disable bottom half first — fewer missing master issues
        work_queue = [{"indices": half_a, "label": "A"}]
        first_test = {"indices": half_b, "label": "B"}

        # Remove cascade plugins from testable — always off during bisection
        cascade_lower = {c.lower() for c in cascade}
        testable_active = [p for p in testable if p.lower() not in cascade_lower]

        state = {
            "base_plugins": base,
            "all_testable": testable_active,
            "groups": groups,
            "cascade": cascade,
            "all_deps": deps,
            "all_masters": all_masters,
            "plugin_to_mod": self._plugin_to_mod,
            "phase": "testing",
            "baseline_fps": baseline_fps,
            "all_on_fps": all_on_fps,
            "work_queue": work_queue,
            "culprits": [],
            "disabled_indices": half_b,
            "current_test": first_test,
            "round": 0,
            "history": [],
        }

        enabled, actual_disabled, protected = self._compute_enabled(state, half_b)
        if protected:
            self.append_log("Protected {} masters from being disabled: {}".format(
                len(protected), ", ".join(protected[:10])))
        self.write_plugins(base, enabled)
        self.save_state(state)

        total_testable = sum(len(g) for g in groups)
        msg = "{} suspects imported, {} testable in {} groups.\nDisabling A ({} plugins off, {} still on).\nLaunch game and report result.".format(
            len(testable), total_testable, len(groups), len(actual_disabled), len(enabled))
        self.append_log(msg)
        return state, msg

    def report_fps(self, fps):
        """Report FPS result for current subtractive test.

        Subtractive logic:
        - Good FPS (cost <= 5): removing the disabled group FIXED the problem.
          Culprits are IN the disabled group. Split it further.
        - Bad FPS (cost > 5): removing the disabled group DIDN'T HELP.
          The disabled group is CLEAN. Move on.
        """
        state = self.load_state()
        if not state:
            return None, "No bisection in progress."

        groups = state["groups"]
        base = state["base_plugins"]
        all_testable = state.get("all_testable", [])
        all_masters_map = state.get("all_masters", {})
        base_set = {p.lower() for p in base}
        self._plugin_to_mod = state.get("plugin_to_mod", {})
        phase = state["phase"]

        if phase != "testing":
            return state, "Bisection is not in testing phase."

        baseline = state["baseline_fps"]
        test = state["current_test"]
        indices = test["indices"]
        fps_cost = baseline - fps

        state["round"] += 1
        self.append_log("Round {} -- Disabled {} = {} FPS (cost: {} FPS, {} groups, {} plugins off)".format(
            state["round"], test["label"], fps, fps_cost, len(indices),
            sum(len(groups[i]) for i in indices)))

        # Log disabled plugins when group is small enough
        test_plugins = []
        for i in indices:
            for p in groups[i]:
                test_plugins.append(p)
        if len(test_plugins) <= 50:
            self.append_log("  Disabled: {}".format(", ".join(test_plugins)))

        state["history"].append({
            "round": state["round"],
            "label": test["label"],
            "groups": len(indices),
            "plugins": len(test_plugins),
            "fps": fps,
            "cost": fps_cost,
        })

        # SUBTRACTIVE: Good FPS means culprits are in the disabled group
        if fps_cost <= 5:
            # Good FPS — removing this group fixed the problem
            can_split = False
            if len(indices) > self.THRESHOLD:
                sub_a, sub_b = self._split_by_plugin_count(indices, groups)
                if sub_b:
                    can_split = True
                    # Test bottom (B) before top (A) — fewer master issues
                    state["work_queue"].append(
                        {"indices": sub_b, "label": test["label"] + ".B"})
                    state["work_queue"].append(
                        {"indices": sub_a, "label": test["label"] + ".A"})
                    a_count = sum(len(groups[i]) for i in sub_a)
                    b_count = sum(len(groups[i]) for i in sub_b)
                    self.append_log("  GOOD FPS -> culprits in this group, splitting into {}.B ({} plugins) and {}.A ({} plugins)".format(
                        test["label"], b_count, test["label"], a_count))

            if not can_split:
                # Can't split further — this IS the culprit
                group_names = []
                total_in_group = 0
                for i in indices:
                    g = groups[i]
                    total_in_group += len(g)
                    if len(g) == 1:
                        group_names.append(g[0])
                    else:
                        group_names.append("{} (+{})".format(g[0], len(g) - 1))
                state["culprits"].append({
                    "indices": indices,
                    "names": group_names,
                    "fps_cost": fps_cost,
                })
                if total_in_group > 20:
                    self.append_log("  CULPRIT GROUP: {} ({} plugins)".format(
                        ", ".join(group_names), total_in_group))
                    self.append_log("  NOTE: Large group -- add '{}' to excludes and re-run to bisect inside it".format(
                        groups[indices[0]][0]))
                else:
                    self.append_log("  CULPRIT: {} (removing fixes FPS)".format(
                        ", ".join(group_names)))
        else:
            # Bad FPS — removing this group didn't help, it's clean
            self.append_log("  BAD FPS -> this group is CLEAN, culprits are elsewhere")

        # Next task
        if state["work_queue"]:
            next_test = state["work_queue"].pop(0)
            next_indices = next_test["indices"]

            enabled, actual_disabled, protected = self._compute_enabled(state, next_indices)
            if protected:
                self.append_log("  Protected {} masters: {}".format(
                    len(protected), ", ".join(protected[:5])))
            self.write_plugins(base, enabled)
            state["current_test"] = next_test
            state["disabled_indices"] = next_indices
            self.save_state(state)

            disabled_count = len(actual_disabled)
            if next_test["label"].endswith(".A"):
                half_desc = "first half"
            elif next_test["label"].endswith(".B"):
                half_desc = "second half"
            else:
                half_desc = "half"
            msg = "Disabling {}: {} off ({} plugins disabled, {} still on).\n{} tests remaining.\nIf GOOD FPS -> culprits in disabled group.\nIf BAD FPS -> disabled group is clean.".format(
                next_test["label"], half_desc, disabled_count, len(enabled),
                len(state["work_queue"]))
            self.append_log(msg)
            return state, msg
        else:
            # All done — re-enable everything
            self.write_plugins(base, all_testable)
            state["phase"] = "done"
            state["disabled_indices"] = []
            self.save_state(state)

            if state["culprits"]:
                self.append_log("=== RESULTS ===")
                total_found = 0
                for c in sorted(state["culprits"], key=lambda x: x["fps_cost"] if isinstance(x["fps_cost"], (int, float)) else 9999, reverse=True):
                    self.append_log("  CULPRIT: {}".format(", ".join(c["names"])))
                    if isinstance(c["fps_cost"], (int, float)):
                        total_found += c["fps_cost"]
                self.append_log("Found {} culprit(s).".format(len(state["culprits"])))
                msg = "Done! Found {} culprit(s). Check the log.".format(
                    len(state["culprits"]))
                self._auto_save_log()
            else:
                msg = "Done. No single group causes the FPS drop alone.\nThe problem may be cumulative (too many plugins total)."
                self.append_log(msg)

            return state, msg

    def report_crash(self):
        """Handle crash: split the crashed group into smaller halves instead of
        quarantining immediately. Only quarantine if the group can't be split
        (single group) or has already crashed twice at this level."""
        state = self.load_state()
        if not state:
            return None, "No bisection in progress."

        groups = state["groups"]
        base = state["base_plugins"]
        all_testable = state.get("all_testable", [])
        all_masters_map = state.get("all_masters", {})
        base_set = {p.lower() for p in base}
        self._plugin_to_mod = state.get("plugin_to_mod", {})

        if state.get("phase") != "testing":
            return state, "Bisection is not in testing phase."

        test = state["current_test"]
        indices = test["indices"]

        state["round"] += 1

        crash_plugins = []
        for i in indices:
            for p in groups[i]:
                crash_plugins.append(p)

        state["history"].append({
            "round": state["round"],
            "label": test["label"],
            "groups": len(indices),
            "plugins": len(crash_plugins),
            "fps": "CRASH",
            "cost": "CRASHED",
        })

        # Try to split instead of quarantine (unless too small to split)
        can_split = False
        if len(indices) > self.THRESHOLD:
            sub_a, sub_b = self._split_by_plugin_count(indices, groups)
            if sub_b:
                can_split = True
                a_count = sum(len(groups[i]) for i in sub_a)
                b_count = sum(len(groups[i]) for i in sub_b)
                self.append_log("Round {} -- Disabled {} CRASHED ({} plugins off) -> splitting into smaller halves".format(
                    state["round"], test["label"], len(crash_plugins)))
                self.append_log("  Trying {}.B ({} plugins) and {}.A ({} plugins) separately".format(
                    test["label"], b_count, test["label"], a_count))
                # Test bottom (B) before top (A) — fewer master issues
                state["work_queue"].append(
                    {"indices": sub_b, "label": test["label"] + ".B"})
                state["work_queue"].append(
                    {"indices": sub_a, "label": test["label"] + ".A"})

        if not can_split:
            # Can't split further or crashed twice — quarantine
            group_names = []
            for i in indices:
                g = groups[i]
                if len(g) == 1:
                    group_names.append(g[0])
                else:
                    group_names.append("{} (+{})".format(g[0], len(g) - 1))

            state["culprits"].append({
                "indices": indices,
                "names": group_names,
                "fps_cost": "CRASHED",
            })

            self.append_log("Round {} -- Disabled {} CRASHED ({} plugins off) -> QUARANTINED".format(
                state["round"], test["label"], len(crash_plugins)))
            if len(crash_plugins) <= 50:
                self.append_log("  Quarantined: {}".format(", ".join(crash_plugins)))
            else:
                self.append_log("  Quarantined: {} plugins (too many to list)".format(
                    len(crash_plugins)))

        # Move to next test
        if state["work_queue"]:
            next_test = state["work_queue"].pop(0)
            next_indices = next_test["indices"]

            enabled, actual_disabled, protected = self._compute_enabled(state, next_indices)
            if protected:
                self.append_log("  Protected {} masters: {}".format(
                    len(protected), ", ".join(protected[:5])))
            self.write_plugins(base, enabled)
            state["current_test"] = next_test
            state["disabled_indices"] = next_indices
            self.save_state(state)

            if next_test["label"].endswith(".A"):
                half_desc = "first half"
            elif next_test["label"].endswith(".B"):
                half_desc = "second half"
            else:
                half_desc = "half"
            action = "Split crashed group" if can_split else "Crashed group quarantined"
            msg = "{}.\nDisabling {}: {} ({} plugins off, {} on).\n{} tests remaining.".format(
                action, next_test["label"], half_desc, len(actual_disabled), len(enabled),
                len(state["work_queue"]))
            self.append_log(msg)
            return state, msg
        else:
            self.write_plugins(base, all_testable)
            state["phase"] = "done"
            state["disabled_indices"] = []
            self.save_state(state)

            if state["culprits"]:
                self.append_log("=== RESULTS ===")
                for c in sorted(state["culprits"], key=lambda x: x["fps_cost"] if isinstance(x["fps_cost"], (int, float)) else 9999, reverse=True):
                    self.append_log("  {}: {}".format(
                        c["fps_cost"], ", ".join(c["names"])))
                msg = "Done! Found {} suspect(s). Check the log.".format(
                    len(state["culprits"]))
                self._auto_save_log()
            else:
                msg = "Done. No culprits found."
                self.append_log(msg)

            return state, msg

    def restore(self):
        self.restore_backups()
        self.append_log("=== Restored original files ===")
        return "Restored. Profile is back to pre-bisection state."

    def get_status_text(self):
        state = self.load_state()
        if not state:
            return "No bisection in progress.\nClick 'Start Bisection' to begin."

        lines = []
        phase = state.get("phase", "?")
        groups = state.get("groups", [])
        total_plugins = sum(len(g) for g in groups)
        lines.append("Plugins: {} base | {} testable in {} groups".format(
            len(state.get("base_plugins", [])), total_plugins, len(groups)))
        lines.append("Round: {}".format(state["round"]))

        if phase == "testing":
            test = state.get("current_test", {})
            n_groups = len(test.get("indices", []))
            disabled_count = sum(len(groups[i]) for i in test.get("indices", []) if i < len(groups))
            lines.append("Phase: Disabling '{}' ({} groups, {} plugins off)".format(
                test.get("label", "?"), n_groups, disabled_count))
            lines.append("Baseline: {} FPS | All on: {} FPS".format(
                state.get("baseline_fps", "?"), state.get("all_on_fps", "?")))
            lines.append("Tests remaining: {}".format(len(state.get("work_queue", []))))
            if state.get("culprits"):
                lines.append("")
                lines.append("Suspects so far ({}):".format(len(state["culprits"])))
                for c in state["culprits"]:
                    cost = c["fps_cost"]
                    all_plugins = []
                    for i in c.get("indices", []):
                        if i < len(groups):
                            for p in groups[i]:
                                all_plugins.append(p)
                    if cost == "CRASHED":
                        lines.append("  CRASHED: {} ({} plugins)".format(
                            c["names"][0] if c["names"] else "?", len(all_plugins)))
                    else:
                        lines.append("  CULPRIT: {} ({} plugins)".format(
                            c["names"][0] if c["names"] else "?", len(all_plugins)))
                lines.append("(Use 'Copy Suspects' for full list)")
        elif phase == "done":
            lines.append("Phase: COMPLETE")
            lines.append("Baseline: {} FPS | All on: {} FPS".format(
                state.get("baseline_fps", "?"), state.get("all_on_fps", "?")))
            if state.get("culprits"):
                lines.append("")
                lines.append("FPS Killers Found:")
                for c in sorted(state["culprits"], key=lambda x: x["fps_cost"] if isinstance(x["fps_cost"], (int, float)) else 9999, reverse=True):
                    total_in = sum(len(groups[i]) for i in c.get("indices", []) if i < len(groups))
                    if total_in > 20:
                        lines.append("  CULPRIT: {} ({} plugins -- add root to excludes, re-run)".format(
                            c["names"][0] if c["names"] else "?", total_in))
                    else:
                        lines.append("  CULPRIT: {}".format(", ".join(c["names"])))
                lines.append("")
                lines.append("Use 'Copy Suspects' or 'Save Log' for full details.")

        if state.get("history"):
            lines.append("")
            lines.append("Summary:")
            for h in state["history"]:
                fps = h.get("fps", "?")
                label = h.get("label", "?")
                n_plugins = h.get("plugins", h.get("groups", "?"))
                if fps == "CRASH":
                    lines.append("  R{}: disabled {} ({} plugins) -> CRASHED".format(
                        h.get("round", "?"), label, n_plugins))
                elif h.get("cost", 0) == 0 or (isinstance(h.get("cost"), (int, float)) and h["cost"] <= 5):
                    lines.append("  R{}: disabled {} ({} plugins) -> {} FPS GOOD (culprits here!)".format(
                        h.get("round", "?"), label, n_plugins, fps))
                else:
                    lines.append("  R{}: disabled {} ({} plugins) -> {} FPS BAD (group is clean)".format(
                        h.get("round", "?"), label, n_plugins, fps))

        return "\n".join(lines)


class ModBisectDialog(QDialog):
    def __init__(self, engine, organizer, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.organizer = organizer
        self.setWindowTitle("Mod Bisect v4 - Find FPS Killers")
        self.setMinimumWidth(580)
        self.setMinimumHeight(560)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Status
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setFont(QFont("Consolas", 10))
        self.status_label.setStyleSheet(
            "background: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 6px;")
        layout.addWidget(self.status_label)

        # Setup group — baseline FPS inputs
        setup_group = QGroupBox("Start Bisection")
        setup_layout = QHBoxLayout(setup_group)
        setup_layout.addWidget(QLabel("Good FPS:"))
        self.baseline_input = QSpinBox()
        self.baseline_input.setRange(1, 999)
        self.baseline_input.setValue(150)
        self.baseline_input.setToolTip("FPS with no mods / known good state")
        self.baseline_input.setStyleSheet("font-size: 14px; padding: 4px; min-width: 70px;")
        setup_layout.addWidget(self.baseline_input)
        setup_layout.addWidget(QLabel("Bad FPS:"))
        self.allon_input = QSpinBox()
        self.allon_input.setRange(1, 999)
        self.allon_input.setValue(50)
        self.allon_input.setToolTip("FPS with full load order")
        self.allon_input.setStyleSheet("font-size: 14px; padding: 4px; min-width: 70px;")
        setup_layout.addWidget(self.allon_input)
        self.setup_btn = QPushButton("Start")
        self.setup_btn.setStyleSheet("font-size: 13px; padding: 8px;")
        self.setup_btn.clicked.connect(self._on_setup)
        setup_layout.addWidget(self.setup_btn)
        self.import_btn = QPushButton("Import List")
        self.import_btn.setToolTip("Load a .txt file of plugin names to bisect only those")
        self.import_btn.setStyleSheet("font-size: 13px; padding: 8px;")
        self.import_btn.clicked.connect(self._on_import)
        setup_layout.addWidget(self.import_btn)
        layout.addWidget(setup_group)

        # Report result buttons
        result_group = QGroupBox("Report Result")
        result_layout = QHBoxLayout(result_group)
        self.good_btn = QPushButton("Good FPS")
        self.good_btn.setToolTip("FPS improved! Culprits are in the disabled group.")
        self.good_btn.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 14px;"
            "background-color: #40a02b; color: white; border-radius: 6px;")
        self.good_btn.clicked.connect(self._on_good)
        result_layout.addWidget(self.good_btn)
        self.bad_btn = QPushButton("Bad FPS")
        self.bad_btn.setToolTip("Still bad FPS. Disabled group is clean.")
        self.bad_btn.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 14px;"
            "background-color: #df8e1d; color: white; border-radius: 6px;")
        self.bad_btn.clicked.connect(self._on_bad)
        result_layout.addWidget(self.bad_btn)
        self.crash_btn = QPushButton("Crashed")
        self.crash_btn.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 14px;"
            "background-color: #d20f39; color: white; border-radius: 6px;")
        self.crash_btn.clicked.connect(self._on_crash)
        result_layout.addWidget(self.crash_btn)
        layout.addWidget(result_group)

        # Suspects actions
        suspects_layout = QHBoxLayout()
        self.copy_suspects_btn = QPushButton("Copy Suspects")
        self.copy_suspects_btn.setStyleSheet("font-size: 11px; padding: 6px;")
        self.copy_suspects_btn.clicked.connect(self._copy_suspects)
        suspects_layout.addWidget(self.copy_suspects_btn)
        self.disable_suspects_btn = QPushButton("Disable Suspects")
        self.disable_suspects_btn.setToolTip("Disable all suspect plugins in plugins.txt")
        self.disable_suspects_btn.setStyleSheet("font-size: 11px; padding: 6px;")
        self.disable_suspects_btn.clicked.connect(self._disable_suspects)
        suspects_layout.addWidget(self.disable_suspects_btn)
        self.rebisect_btn = QPushButton("Re-bisect Suspects")
        self.rebisect_btn.setToolTip("Restore, exclude the group root, and re-run to split large groups")
        self.rebisect_btn.setStyleSheet(
            "font-size: 11px; padding: 6px; font-weight: bold;"
            "background-color: #7c3aed; color: white; border-radius: 4px;")
        self.rebisect_btn.clicked.connect(self._rebisect_suspects)
        suspects_layout.addWidget(self.rebisect_btn)
        layout.addLayout(suspects_layout)

        # Restore
        self.restore_btn = QPushButton("Restore Original Files")
        self.restore_btn.setStyleSheet("font-size: 11px; padding: 6px;")
        self.restore_btn.clicked.connect(self._on_restore)
        layout.addWidget(self.restore_btn)

        # Collapsible log section
        self.log_toggle = QPushButton(">> Test Log")
        self.log_toggle.setStyleSheet(
            "font-weight: bold; font-size: 11px; padding: 4px; text-align: left; border: none;")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(False)
        self.log_toggle.clicked.connect(self._toggle_log)
        layout.addWidget(self.log_toggle)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet(
            "background: #1e1e2e; color: #a6adc8; padding: 6px; border-radius: 4px;")
        self.log_text.setVisible(False)
        layout.addWidget(self.log_text)

        log_btns = QHBoxLayout()
        copy_btn = QPushButton("Copy Log")
        copy_btn.setStyleSheet("font-size: 11px; padding: 5px;")
        copy_btn.clicked.connect(self._copy_log)
        self.copy_log_btn = copy_btn
        self.copy_log_btn.setVisible(False)
        log_btns.addWidget(self.copy_log_btn)
        save_btn = QPushButton("Save Log to Desktop")
        save_btn.setStyleSheet("font-size: 11px; padding: 5px;")
        save_btn.clicked.connect(self._save_to_desktop)
        self.save_log_btn = save_btn
        self.save_log_btn.setVisible(False)
        log_btns.addWidget(self.save_log_btn)
        layout.addLayout(log_btns)

    def _refresh(self):
        state = self.engine.load_state()
        has_state = state is not None
        phase = state.get("phase", "") if state else ""
        in_progress = has_state and phase not in ("done", "")

        self.setup_btn.setEnabled(not has_state)
        self.import_btn.setEnabled(not has_state)
        self.baseline_input.setEnabled(not has_state)
        self.allon_input.setEnabled(not has_state)
        self.good_btn.setEnabled(in_progress)
        self.bad_btn.setEnabled(in_progress)
        self.crash_btn.setEnabled(in_progress)
        self.restore_btn.setEnabled(has_state)

        has_suspects = bool(state and state.get("culprits"))
        is_done = has_state and phase == "done"
        self.copy_suspects_btn.setEnabled(has_suspects)
        self.disable_suspects_btn.setEnabled(has_suspects)
        # Re-bisect only when done and has large suspect groups
        has_large = False
        if has_suspects:
            groups_data = state.get("groups", [])
            for c in state["culprits"]:
                total = sum(len(groups_data[i]) for i in c.get("indices", []) if i < len(groups_data))
                if total > 20:
                    has_large = True
                    break
        self.rebisect_btn.setEnabled(is_done and has_large)

        self.status_label.setText(self.engine.get_status_text())
        self.log_text.setPlainText(self.engine.read_log())
        if self.log_text.isVisible():
            QApplication.processEvents()
            sb = self.log_text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Suspect Plugin List", os.path.expanduser("~/Desktop"),
            "Text files (*.txt);;All files (*)")
        if not path:
            return
        baseline = self.baseline_input.value()
        allon = self.allon_input.value()
        if baseline <= allon:
            QMessageBox.warning(self, "Error",
                "Good FPS ({}) must be higher than Bad FPS ({}).".format(baseline, allon))
            return
        # Count lines for confirmation
        with open(path, "r", encoding="utf-8-sig") as f:
            count = sum(1 for line in f if line.strip() and not line.strip().startswith("#"))
        reply = QMessageBox.question(
            self, "Import Suspects",
            "File: {}\nPlugins: {}\nGood FPS: {} | Bad FPS: {}\n\n"
            "Only these plugins will be bisected.\n"
            "Everything else stays enabled as base.\nContinue?".format(
                os.path.basename(path), count, baseline, allon),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        state, msg = self.engine.setup_from_list(baseline, allon, path)
        if state is None:
            QMessageBox.warning(self, "Error", msg)
        else:
            QMessageBox.information(self, "Import Started",
                msg + "\n\nLaunch game and report result.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_setup(self):
        baseline = self.baseline_input.value()
        allon = self.allon_input.value()
        if baseline <= allon:
            QMessageBox.warning(self, "Error",
                "Good FPS ({}) must be higher than Bad FPS ({}).".format(baseline, allon))
            return
        reply = QMessageBox.question(
            self, "Start Bisection",
            "Good FPS: {}  |  Bad FPS: {}\n"
            "FPS cost to find: {} FPS\n\n"
            "This will back up your files and start bisecting\n"
            "your entire load order (subtractive — disables halves).\n"
            "Continue?".format(
                baseline, allon, baseline - allon),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        state, msg = self.engine.setup(baseline, allon)
        if state is None:
            QMessageBox.warning(self, "Error", msg)
        else:
            QMessageBox.information(self, "Ready",
                msg + "\n\nLaunch the game, check your FPS, then\n"
                "come back and click Good or Bad.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_good(self):
        state = self.engine.load_state()
        fps = state["baseline_fps"] if state else 150
        state, msg = self.engine.report_fps(fps)
        if state and state.get("phase") == "done":
            QMessageBox.information(self, "Complete", msg)
        else:
            QMessageBox.information(self, "Next Test",
                msg + "\n\nLaunch the game and report the result.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_bad(self):
        state = self.engine.load_state()
        fps = state["all_on_fps"] if state else 50
        state, msg = self.engine.report_fps(fps)
        if state and state.get("phase") == "done":
            QMessageBox.information(self, "Complete", msg)
        else:
            QMessageBox.information(self, "Next Test",
                msg + "\n\nLaunch the game and report the result.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_crash(self):
        # Check current group size to give appropriate message
        state = self.engine.load_state()
        indices = state.get("current_test", {}).get("indices", []) if state else []
        groups = state.get("groups", []) if state else []
        plugin_count = sum(len(groups[i]) for i in indices if i < len(groups))
        can_split = len(indices) > self.engine.THRESHOLD

        if can_split:
            reply = QMessageBox.question(
                self, "Game Crashed",
                "Retry same test, or split into smaller halves?\n\n"
                "Retry = same plugins disabled, test again\n"
                "Split = try disabling smaller portions instead",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Ignore,
            )
        else:
            reply = QMessageBox.question(
                self, "Game Crashed",
                "Retry same test, or quarantine this group?\n\n"
                "Retry = same plugins disabled, test again\n"
                "Quarantine = mark as suspect, skip to next test",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Ignore,
            )
        if reply == QMessageBox.StandardButton.Retry:
            self.engine.append_log("CRASHED -- Retrying same test")
            QMessageBox.information(self, "Retry",
                "Same plugins still disabled. Launch game and try again.")
        else:
            state, msg = self.engine.report_crash()
            if state and state.get("phase") == "done":
                QMessageBox.information(self, "Complete", msg)
            else:
                QMessageBox.information(self, "Next Test",
                    msg + "\n\nLaunch the game and report result.")
                self._try_refresh_mo2()
        self._refresh()

    def _on_restore(self):
        reply = QMessageBox.question(
            self, "Restore",
            "Restore plugins.txt and modlist.txt to pre-bisection state?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        msg = self.engine.restore()
        self._try_refresh_mo2()
        QMessageBox.information(self, "Restored", msg)
        self._refresh()

    def _try_refresh_mo2(self):
        try:
            self.organizer.refresh()
        except Exception:
            pass

    def _toggle_log(self):
        show = self.log_toggle.isChecked()
        self.log_text.setVisible(show)
        self.copy_log_btn.setVisible(show)
        self.save_log_btn.setVisible(show)
        self.log_toggle.setText("vv Test Log" if show else ">> Test Log")
        if show:
            QApplication.processEvents()
            sb = self.log_text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _get_suspect_plugins(self):
        """Get list of all plugins from culprit groups."""
        state = self.engine.load_state()
        if not state or not state.get("culprits"):
            return []
        groups = state.get("groups", [])
        plugins = []
        for c in state["culprits"]:
            for i in c.get("indices", []):
                if i < len(groups):
                    for p in groups[i]:
                        plugins.append(p)
        return plugins

    def _copy_suspects(self):
        plugins = self._get_suspect_plugins()
        if not plugins:
            QMessageBox.information(self, "No Suspects", "No suspects found yet.")
            return
        text = "Suspect plugins ({}):\n".format(len(plugins))
        for p in plugins:
            text += "  {}\n".format(p)
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Copied",
            "{} suspect plugins copied to clipboard.".format(len(plugins)))

    def _disable_suspects(self):
        plugins = self._get_suspect_plugins()
        if not plugins:
            QMessageBox.information(self, "No Suspects", "No suspects found yet.")
            return
        reply = QMessageBox.question(
            self, "Disable Suspects",
            "Disable {} suspect plugins in plugins.txt?\n\n"
            "This will remove the * prefix from these plugins.\n"
            "Use Restore to undo.".format(len(plugins)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Read plugins.txt, remove * from suspects
        suspect_lower = {p.lower() for p in plugins}
        with open(self.engine.plugins_file, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        with open(self.engine.plugins_file, "w", encoding="utf-8") as f:
            disabled = 0
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("*"):
                    name = stripped[1:]
                    if name.lower() in suspect_lower:
                        f.write("{}\n".format(name))
                        disabled += 1
                        continue
                f.write(line)
        # Sync left pane: read current enabled plugins from the file we just wrote
        remaining_enabled = self.engine.read_enabled_plugins()
        self.engine.sync_modlist(remaining_enabled)
        self._try_refresh_mo2()
        self.engine.append_log("Disabled {} suspect plugins.".format(disabled))
        QMessageBox.information(self, "Done",
            "Disabled {} suspect plugins.".format(disabled))
        self._refresh()

    def _rebisect_suspects(self):
        """Restore, add large group roots to excludes, and restart bisection."""
        state = self.engine.load_state()
        if not state or not state.get("culprits"):
            return
        groups = state.get("groups", [])
        # Find root plugins of large culprit groups
        roots = []
        for c in state["culprits"]:
            total = sum(len(groups[i]) for i in c.get("indices", []) if i < len(groups))
            if total > 20 and c.get("indices"):
                root_group = groups[c["indices"][0]]
                if root_group:
                    roots.append(root_group[0])
        if not roots:
            QMessageBox.information(self, "No Large Groups",
                "No large suspect groups found to re-bisect.")
            return
        # Show what we'll exclude
        root_names = ", ".join(roots)
        reply = QMessageBox.question(
            self, "Re-bisect Suspects",
            "This will:\n"
            "1. Restore original files\n"
            "2. Add these to excludes: {}\n"
            "3. Start a new bisection\n\n"
            "The large groups will break into smaller testable pieces.\n"
            "Continue?".format(root_names),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        baseline = state.get("baseline_fps", 150)
        allon = state.get("all_on_fps", 50)
        # Restore
        self.engine.restore_backups()
        self._try_refresh_mo2()
        # Add roots to exclude patterns
        for r in roots:
            # Use the plugin name without extension as pattern
            name_no_ext = os.path.splitext(r)[0].lower()
            if name_no_ext not in [p.lower() for p in self.engine.exclude_patterns]:
                self.engine.exclude_patterns.append(name_no_ext)
        # Re-run setup
        state, msg = self.engine.setup(baseline, allon)
        if state is None:
            QMessageBox.warning(self, "Error", msg)
        else:
            QMessageBox.information(self, "Re-bisect Started",
                "Excluded: {}\n\n{}\n\nLaunch game and report result.".format(
                    root_names, msg))
            self._try_refresh_mo2()
        self._refresh()

    def _save_to_desktop(self):
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(desktop, "bisect_log_{}.txt".format(ts))
        log = self.engine.read_log()
        # Append suspects summary
        suspects = self._get_suspect_plugins()
        if suspects:
            log += "\n\n=== SUSPECT PLUGINS ({}) ===\n".format(len(suspects))
            for p in suspects:
                log += "  {}\n".format(p)
        with open(path, "w", encoding="utf-8") as f:
            f.write(log)
        QMessageBox.information(self, "Saved", "Log saved to:\n{}".format(path))

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        QMessageBox.information(self, "Copied", "Log copied to clipboard.")


class ModBisectPlugin(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self.__organizer = None
        self.__parent = None

    def init(self, organizer):
        self.__organizer = organizer
        return True

    def name(self):
        return "ModBisect"

    def localizedName(self):
        return self.tr("Mod Bisect Tool")

    def author(self):
        return "Claude"

    def description(self):
        return self.tr(
            "Find FPS-killing plugins via subtractive binary search. "
            "Disables halves to isolate culprits. "
            "Syncs both left pane (mods) and right pane (plugins).")

    def version(self):
        return mobase.VersionInfo(4, 0, 0, 0)

    def settings(self):
        return [
            mobase.PluginSetting(
                "extra_excludes",
                self.tr("Extra mod name patterns to never disable (comma-separated)"),
                ""),
        ]

    def displayName(self):
        return self.tr("Mod Bisect Tool")

    def tooltip(self):
        return self.tr("Find FPS-killing plugins via subtractive binary search")

    def icon(self):
        return QIcon()

    def setParentWidget(self, widget):
        self.__parent = widget

    def display(self):
        org = self.__organizer
        profile_dir = org.profilePath()
        mods_dir = os.path.join(org.basePath(), "mods")

        # Build exclude list
        extra = org.pluginSetting(self.name(), "extra_excludes")
        exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        if extra:
            for p in extra.split(","):
                p = p.strip()
                if p:
                    exclude_patterns.append(p)

        overwrite_dir = org.overwritePath()
        engine = BisectEngine(profile_dir, mods_dir, overwrite_dir, exclude_patterns, organizer=org)
        dlg = ModBisectDialog(engine, org, self.__parent)
        dlg.exec()

    def tr(self, s):
        return QCoreApplication.translate("ModBisectPlugin", s)


def createPlugin():
    return ModBisectPlugin()
