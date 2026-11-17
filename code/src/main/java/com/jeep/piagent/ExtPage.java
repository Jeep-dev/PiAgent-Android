package com.jeep.piagent;

import android.app.Dialog;
import android.text.TextUtils;
import android.widget.LinearLayout;

import java.util.ArrayList;
import java.util.List;

import org.json.JSONArray;
import org.json.JSONObject;

/** Settings → 扩展: skills, prompt templates (Markdown files that are / commands) and the tools. */
final class ExtPage extends Part {
    /** The 扩展 page's counts (skills, prompt templates), read off the main thread; null until read. */
    private int[] extCounts;

    ExtPage(MainActivity app) { super(app); }

    void fillExt(LinearLayout box, JSONObject launch) {
        int[] n = extCounts;
        extCounts = null; // read again next time
        if (n == null) {
            work.execute(() -> {
                int[] read = {app.skills.skillList().size(), templates().size()};
                ui.post(() -> {
                    if (!"ext".equals(app.settings.settingsAt) || app.settings.settingsPage == null) return;
                    extCounts = read;
                    app.settings.fillSettings(app.link.linked ? app.settings.launchCache : null);
                });
            });
        }
        LinearLayout ext = app.settings.card(box);
        sheets.option(ext, R.drawable.ic_spark, "技能", count(n, 0), false, app.skills::skillsSheet);
        sheets.option(ext, R.drawable.ic_edit_lines, "提示模板", count(n, 1), false, this::templatesSheet);
        if (launch != null) {
            int off = disallowed(launch).size();
            sheets.option(ext, R.drawable.ic_tool, "工具", off == 0 ? null : "关闭 " + off, false, () -> toolsSheet(launch));
        }
    }

    private static String count(int[] n, int i) { return n == null || n[i] == 0 ? null : String.valueOf(n[i]); }

    // ---- 提示模板: ~/.pi/agent/prompts/<name>.md and the folder's .pi/prompts, each a / command ----

    /** A template: its name, its file, and where it is when not the user's own. */
    private static final class Item {
        final String name, from;
        final java.io.File file;

        Item(String name, java.io.File file, String from) {
            this.name = name;
            this.file = file;
            this.from = from;
        }
    }

    private java.io.File templatesDir() { return userland.file(Userland.HOME + "/.pi/agent/prompts"); }

    /** The user's templates, then this folder's (pi loads a prompts folder's own .md files, not its subfolders'). */
    private List<Item> templates() {
        List<Item> out = new ArrayList<>();
        java.util.Set<String> seen = new java.util.HashSet<>();
        addItems(out, seen, templatesDir(), null);
        java.io.File here = userland.file(agent.mcpFolder() + "/.pi/prompts");
        if (!here.equals(templatesDir())) addItems(out, seen, here, "本目录");
        return out;
    }

    private static void addItems(List<Item> out, java.util.Set<String> seen, java.io.File dir, String from) {
        java.io.File[] files = dir.listFiles();
        if (files == null) return;
        java.util.Arrays.sort(files);
        for (java.io.File f : files) {
            if (!f.isFile() || !f.getName().endsWith(".md")) continue;
            String n = f.getName().substring(0, f.getName().length() - 3);
            if (seen.add(n)) out.add(new Item(n, f, from));
        }
    }

    private void templatesSheet() {
        Dialog sheet = sheets.sheet("提示模板", null);
        LinearLayout card = sheets.card(sheet);
        List<Item> items = templates();
        for (Item it : items) {
            sheets.option(card, R.drawable.ic_edit_lines, "/" + it.name, it.from, false, () -> { sheet.dismiss(); itemActions(it.file); });
        }
        if (items.isEmpty()) {
            sheets.empty(card, "还没有：模板是写好的提示，用 /名称 发出");
        }
        LinearLayout add = sheets.card(sheet);
        Ui.margins(add, app, 0, 10, 0, 0);
        sheets.option(add, R.drawable.ic_plus, "新建", null, false, () -> {
            sheet.dismiss();
            sheets.textSheet("新建提示模板", "", "名称：小写字母、数字和 -", false, (t, done) -> {
                String n = t.trim();
                if (!n.matches("[a-z0-9][a-z0-9-]*")) { toast("名称只能用小写字母、数字和 -"); return; }
                java.io.File f = new java.io.File(templatesDir(), n + ".md");
                if (f.exists()) { toast("已经有了"); return; }
                String body = "---\ndescription: 一句话说明这个模板做什么\n---\n在这里写 /" + n + " 发出的提示。$ARGUMENTS 是命令后面跟的内容，$1、$2 是其中的第几个。\n";
                try {
                    Userland.write(f, body);
                } catch (java.io.IOException e) {
                    toast("新建失败：" + e.getMessage());
                    return;
                }
                done.run();
                editItem(f);
            }, null);
        });
        sheet.show();
    }

    private void itemActions(java.io.File f) {
        Dialog sheet = sheets.sheet(f.getName().replaceAll("\\.md$", ""), null);
        LinearLayout card = sheets.card(sheet);
        sheets.option(card, R.drawable.ic_pencil, "编辑", null, false, () -> { sheet.dismiss(); editItem(f); });
        sheets.option(card, R.drawable.ic_trash, "删除", null, true, () -> {
            sheet.dismiss();
            f.delete();
            agent.relaunch();
            toast("已删除");
            app.settings.refreshSettings();
            templatesSheet();
        });
        sheet.show();
    }

    private void editItem(java.io.File f) {
        sheets.textSheet(f.getName(), agent.readFile(f), "", true, (t, done) -> {
            try {
                Userland.write(f, t);
            } catch (java.io.IOException e) {
                toast("保存失败：" + e.getMessage());
                return;
            }
            agent.relaunch();
            toast("已保存");
            done.run();
            app.settings.refreshSettings();
        }, this::templatesSheet);
    }

    // ---- 工具: left out with pi's --exclude-tools ----

    private static List<String> disallowed(JSONObject launch) {
        List<String> out = new ArrayList<>();
        for (String t : launch.optString("disallowedTools").replace(',', ' ').trim().split("\\s+")) if (!t.isEmpty()) out.add(t);
        return out;
    }

    private void toolsSheet(JSONObject launch) {
        Dialog sheet = sheets.sheet("工具", null);
        LinearLayout card = sheets.card(sheet);
        JSONArray tools = savedArray("tools");
        List<String> off = disallowed(launch);
        List<String> all = new ArrayList<>();
        for (int i = 0; i < tools.length(); i++) all.add(tools.optString(i));
        for (String t : off) if (!all.contains(t)) all.add(t);
        for (String t : all) {
            boolean on = !off.contains(t);
            String sub = t.startsWith("mcp__") ? t.split("__")[1] : null;
            String label = t.startsWith("mcp__") && t.split("__").length > 2 ? t.substring(t.indexOf("__", 5) + 2) : t;
            sheets.option(card, 0, label, sub, false, () -> {
                List<String> next = new ArrayList<>(off);
                if (on) next.add(t); else next.remove(t);
                agent.saveLaunch("disallowedTools", TextUtils.join(" ", next), () -> {
                    sheet.dismiss();
                    app.settings.refreshSettings();
                    bridge.get("/launch", (status, l) -> { if (status == 200) { app.settings.launchCache = l; toolsSheet(l); } });
                });
            });
            if (on) sheets.check(card);
        }
        if (all.isEmpty()) {
            sheets.empty(card, "发一条消息后显示");
        }
        sheet.show();
    }
}
