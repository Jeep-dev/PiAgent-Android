package com.jeep.piagent;

import android.content.SharedPreferences;
import android.os.Handler;

import java.util.concurrent.ExecutorService;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * What the parts of the screen share. MainActivity keeps the state of the app (the link to the bridge, the
 * conversations) and is made of parts, each a piece of the interface or of what it does. A part is built for
 * the activity (app), reaches the others through it, and has by name what nearly all of them use.
 */
abstract class Part {
    final MainActivity app;
    final Bridge bridge;
    final Userland userland;
    final SharedPreferences prefs;
    final Handler ui;
    final ExecutorService work;
    /** What pi is here, and the sheets' building blocks. Made first: Claude has neither of them, Sheets has no sheets. */
    final Agent agent;
    final Sheets sheets;

    Part(MainActivity app) {
        this.app = app;
        bridge = app.bridge;
        userland = app.userland;
        prefs = app.prefs;
        ui = app.ui;
        work = app.work;
        agent = app.agent;
        sheets = app.sheets;
        // They are made first (MainActivity.onCreate): a part made before them would keep a null.
        boolean isAgent = this instanceof Agent, isSheets = this instanceof Sheets;
        if ((agent == null && !isAgent) || (sheets == null && !isAgent && !isSheets)) {
            throw new IllegalStateException(getClass().getSimpleName() + " is made before Agent and Sheets");
        }
    }

    int dp(float v) { return Ui.dp(app, v); }

    int col(int res) { return Ui.color(app, res); }

    void toast(String text) { app.toast(text); }

    /** A JSON object from keys and values (a null value as JSON null). */
    static JSONObject obj(Object... kv) {
        JSONObject o = new JSONObject();
        try {
            for (int i = 0; i < kv.length; i += 2) o.put((String) kv[i], kv[i + 1] == null ? JSONObject.NULL : kv[i + 1]);
        } catch (JSONException ignored) {
            // keys are strings
        }
        return o;
    }

    /** A list kept in the preferences as JSON (the tools pi reported, the pinned conversations). */
    JSONArray savedArray(String key) {
        try {
            return new JSONArray(prefs.getString(key, "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }
}
