package com.jeep.piagent;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** What this app knows of pi: what the bridge reported of it, what is picked here (model, thinking level, folder), and its settings.json. */
final class Agent extends Part {
    /** pi's version, as the bridge reported it. */
    String claudeVersion = "";
    /** From pi itself (its --help and settings.json): thinking levels, permission modes, the level set. */
    final List<String> efforts = new ArrayList<>(), permissionModes = new ArrayList<>();
    /** The thinking level in pi's own settings. */
    private String configEffort = "";
    /**
     * The userland's home and the folder pi starts in (from the bridge); what is picked here: the folder
     * for new conversations, the model and the thinking level (empty: pi's own); its default model.
     */
    String home = "", defaultCwd = "", cwd, model, effort, defaultModel = "";
    /** The models pi's settings name. */
    final List<String> models = new ArrayList<>();
    /** The / commands pi has (kept for the next start, when the box offers them before it has reported). */
    final List<String> commands = new ArrayList<>();
    /** pi's own model list (as its /model shows it): value, name. */
    JSONArray claudeModels = new JSONArray();
    /** Some model can be used (a provider pi can sign in to): false until the bridge says so, and when none can. */
    boolean auth = true;
    /** settings.json is there but did not read as JSON: a change saved over it would lose it. */
    boolean settingsBroken;

    Agent(MainActivity app) {
        super(app);
        cwd = prefs.getString("cwd", "");
        model = prefs.getString("model", "");
        effort = prefs.getString("effort", "");
        try {
            fill(efforts, new JSONArray(prefs.getString("efforts", "[]")));
            fill(permissionModes, new JSONArray(prefs.getString("permissionModes", "[]")));
            claudeModels = new JSONArray(prefs.getString("claudeModels", "[]"));
        } catch (JSONException ignored) {
            // none saved
        }
        try {
            JSONArray saved = new JSONArray(prefs.getString("commands", "[]"));
            for (int i = 0; i < saved.length(); i++) commands.add(saved.optString(i));
        } catch (JSONException ignored) {
            // none saved
        }
    }

    /** Whether pi's default (no model chosen) is this model. */
    boolean defaultResolved(String resolved) {
        for (int i = 0; i < claudeModels.length(); i++) {
            JSONObject cm = claudeModels.optJSONObject(i);
            if (cm != null && cm.optString("value").equals("default")) return !resolved.isEmpty() && resolved.equals(cm.optString("resolvedModel"));
        }
        return false;
    }

    /** Takes what the bridge read from pi and its settings. */
    void applyConfig(JSONObject data) {
        if (data.optJSONArray("providers") != null) prefs.edit().putString("providers", data.optJSONArray("providers").toString()).apply();
        fill(models, data.optJSONArray("models"));
        JSONArray cm = data.optJSONArray("claudeModels");
        if (cm != null && cm.length() > 0) {
            claudeModels = cm;
            prefs.edit().putString("claudeModels", cm.toString()).apply();
        }
        defaultModel = data.optString("defaultModel");
        auth = data.optBoolean("auth", true);
        claudeVersion = data.optString("claudeVersion");
        configEffort = data.optString("effort").toLowerCase(Locale.ROOT);
        // Found a moment after the bridge starts: until then, the last ones it gave.
        if (data.optJSONArray("efforts") != null && data.optJSONArray("efforts").length() > 0) {
            fill(efforts, data.optJSONArray("efforts"));
            fill(permissionModes, data.optJSONArray("permissionModes"));
            prefs.edit().putString("efforts", data.optJSONArray("efforts").toString())
                    .putString("permissionModes", String.valueOf(data.optJSONArray("permissionModes"))).apply();
        }
        app.screen.updateChrome();
    }

    private static void fill(List<String> to, JSONArray from) {
        to.clear();
        for (int i = 0; from != null && i < from.length(); i++) to.add(from.optString(i));
    }

    /** The folder new conversations start in (Settings → 对话), pi's default until one is picked. */
    String workFolder() {
        return cwd.isEmpty() ? defaultCwd : cwd;
    }

    String tilde(String p) {
        if (p == null) return "";
        return !home.isEmpty() && p.startsWith(home) ? "~" + p.substring(home.length()) : p;
    }

    /** The thinking level in use: picked here, else pi's own setting. */
    String effortInUse() {
        return !effort.isEmpty() ? effort : configEffort;
    }

    String readFile(java.io.File f) {
        try {
            return Userland.read(new java.io.FileInputStream(f));
        } catch (java.io.IOException e) {
            return "";
        }
    }

    JSONObject readJson(java.io.File f) {
        try {
            return new JSONObject(readFile(f));
        } catch (JSONException e) {
            return new JSONObject();
        }
    }

    /** A change pi reads when it starts: new conversations start with it. */
    void relaunch() {
        if (app.link.linked) bridge.post("/launch", new JSONObject(), null);
    }

    String mcpFolder() {
        String f = app.current != null && app.current.cwd != null && !app.current.cwd.isEmpty() ? app.current.cwd : cwd;
        return f.isEmpty() ? defaultCwd : f;
    }

    // ---- settings.json, read and written here directly ----

    java.io.File settingsFile() { return userland.file(Userland.HOME + "/.pi/agent/settings.json"); }

    JSONObject settingsJson() {
        settingsBroken = false;
        java.io.File f = settingsFile();
        if (!f.exists()) return new JSONObject();
        try {
            return new JSONObject(Userland.read(new java.io.FileInputStream(f)));
        } catch (Exception e) {
            settingsBroken = true;
            return new JSONObject();
        }
    }

    JSONObject settingsEnv() {
        JSONObject env = settingsJson().optJSONObject("env");
        return env != null ? env : new JSONObject();
    }

    /** Writes settings.json; new conversations start with it. */
    boolean saveSettings(JSONObject json) {
        if (settingsBroken) {
            toast("settings.json 格式有错，没有保存");
            return false;
        }
        try {
            Userland.replace(settingsFile(), json.toString(2) + "\n");
        } catch (Exception e) {
            toast("保存失败：" + e.getMessage());
            return false;
        }
        if (app.link.linked) {
            bridge.post("/launch", new JSONObject(), null); // the spare is started again with it
            bridge.get("/config", (status, data) -> {
                if (status != 200) return;
                applyConfig(data);
                app.renderNow();
            });
        }
        return true;
    }

    void saveLaunch(String key, String value, Runnable done) {
        bridge.post("/launch", obj(key, value), (status, data) -> {
            if (status == 200) {
                toast("已保存");
                done.run();
            } else {
                toast(status == 400 ? "参数有误：" + data.optString("error") : "保存失败");
            }
        });
    }
}
