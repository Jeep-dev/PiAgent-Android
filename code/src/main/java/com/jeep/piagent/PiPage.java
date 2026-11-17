package com.jeep.piagent;

import android.app.Dialog;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Settings → 模型服务: how pi signs in to model providers (an API key per provider, kept in pi's own
 * auth.json by the bridge; a subscription's sign-in with pi's /login in the terminal), the models
 * those give, and pi's own files for the rest: models.json (endpoints of one's own) and settings.json.
 */
final class PiPage extends Part {
    /** Providers pi has built in that sign in with an API key: id, name shown. */
    private static final String[][] PROVIDERS = {
            {"anthropic", "Anthropic"}, {"openai", "OpenAI"}, {"google", "Google Gemini"}, {"deepseek", "DeepSeek"},
            {"openrouter", "OpenRouter"}, {"moonshotai-cn", "Kimi（月之暗面，国内）"}, {"zai-coding-cn", "智谱 GLM Coding（国内）"},
            {"qwen-token-plan-cn", "通义千问（国内）"}, {"minimax-cn", "MiniMax（国内）"}, {"xiaomi", "小米 MiMo"},
            {"xai", "xAI"}, {"mistral", "Mistral"}, {"groq", "Groq"}};

    PiPage(MainActivity app) { super(app); }

    /** pi's own files, edited here directly. */
    private java.io.File agentFile(String name) { return userland.file(Userland.HOME + "/.pi/agent/" + name); }

    void fillPi(LinearLayout box) {
        List<String> keyed = keyedProviders();
        app.settings.section(box, "API Key");
        LinearLayout keys = app.settings.card(box);
        List<String> listed = new ArrayList<>();
        for (String[] p : PROVIDERS) {
            listed.add(p[0]);
            sheets.option(keys, 0, p[1], keyed.contains(p[0]) ? "已填" : null, false, () -> keySheet(p[0], p[1]));
        }
        for (String id : keyed) {
            if (!listed.contains(id)) sheets.option(keys, 0, id, "已填", false, () -> keySheet(id, id));
        }
        sheets.option(keys, R.drawable.ic_plus, "其他服务商", null, false, () ->
                sheets.textSheet("服务商 ID", "", "如 fireworks、together（pi 的服务商名）", false, (t, done) -> {
                    String id = t.trim();
                    if (!id.matches("[\\w.-]{1,60}")) { toast("只能用字母、数字和 - . _"); return; }
                    done.run();
                    keySheet(id, id);
                }, null));

        app.settings.section(box, "订阅登录");
        LinearLayout login = app.settings.card(box);
        sheets.option(login, R.drawable.ic_terminal, "在终端里登录", "Claude Pro/Max、ChatGPT、Copilot 等", false, () -> {
            toast("在终端里运行 pi，输入 /login 选择服务商；登录后回来发消息即可");
            app.openTerminal();
        });

        app.settings.section(box, "模型");
        LinearLayout models = app.settings.card(box);
        int n = agent.claudeModels.length();
        sheets.option(models, R.drawable.ic_spark, "可用模型", n == 0 ? "还没有：先填 API Key" : String.valueOf(n), false, this::modelsSheet);
        sheets.option(models, R.drawable.ic_file, "models.json", "自己的端点（Ollama、代理等）", false, () -> rawSheet("models.json"));
        sheets.option(models, R.drawable.ic_gear, "settings.json", "pi 的设置", false, () -> rawSheet("settings.json"));
    }

    /** The providers with an API key kept in auth.json (as the bridge last reported). */
    private List<String> keyedProviders() {
        List<String> out = new ArrayList<>();
        JSONArray a = savedArray("providers");
        for (int i = 0; i < a.length(); i++) if (!a.optString(i).isEmpty()) out.add(a.optString(i));
        return out;
    }

    private void keySheet(String id, String name) {
        if (!app.link.linked) { toast("pi 还没启动好"); return; }
        boolean has = keyedProviders().contains(id);
        Dialog sheet = sheets.sheet(name, has ? "已经填过；填新的会替换它" : "填好后，这家的模型会出现在模型列表里");
        EditText key = sheets.field(sheet, "API Key", "", has ? "（不改就留空）" : "sk-…");
        TextView remove = sheets.button(has ? "删除" : "取消", false, v -> {
            if (!has) { sheet.dismiss(); return; }
            save(id, "", sheet);
        });
        TextView ok = sheets.button("保存", true, v -> {
            String k = key.getText().toString().trim();
            if (k.isEmpty()) { sheet.dismiss(); return; }
            save(id, k, sheet);
        });
        ok.setBackground(Ui.press(app, Ui.round(col(R.color.text), dp(10)), dp(10)));
        ok.setTextColor(col(R.color.bg));
        sheets.actions(sheet, remove, ok);
        if (sheet.getWindow() != null) sheet.getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        sheet.show();
    }

    private void save(String id, String key, Dialog sheet) {
        bridge.post("/auth", obj("provider", id, "key", key), (status, data) -> {
            if (status != 200) { toast("保存失败：" + data.optString("error", "HTTP " + status)); return; }
            toast(key.isEmpty() ? "已删除" : "已保存，正在读取这家的模型…");
            sheet.dismiss();
            refreshConfig(3);
        });
    }

    /** The bridge reads pi's models again a moment after a change: asked a few times until it has. */
    void refreshConfig(int tries) {
        bridge.get("/config", (status, data) -> {
            if (status != 200) return;
            int before = agent.claudeModels.length();
            agent.applyConfig(data);
            app.settings.refreshSettings();
            if (tries > 1 && agent.claudeModels.length() == before) ui.postDelayed(() -> refreshConfig(tries - 1), 2500);
        });
    }

    private void modelsSheet() {
        Dialog sheet = sheets.sheet("可用模型", "有 API Key 或已登录的服务商的模型；在输入框里用 /model 选");
        LinearLayout card = sheets.card(sheet);
        for (int i = 0; i < agent.claudeModels.length(); i++) {
            JSONObject m = agent.claudeModels.optJSONObject(i);
            if (m != null) sheets.option(card, 0, m.optString("displayName"), m.optString("value"), false, null);
        }
        if (agent.claudeModels.length() == 0) sheets.empty(card, "还没有");
        sheet.show();
    }

    /** One of pi's JSON files, edited whole; one that does not read as JSON is not saved. */
    private void rawSheet(String name) {
        java.io.File f = agentFile(name);
        String text = f.exists() ? agent.readFile(f) : "{\n}\n";
        sheets.textSheet(name, text, "{ }", true, (t, done) -> {
            try {
                new JSONObject(t.trim().isEmpty() ? "{}" : t);
            } catch (JSONException e) {
                toast("不是有效的 JSON：" + e.getMessage());
                return;
            }
            try {
                Userland.replace(f, t.endsWith("\n") ? t : t + "\n");
            } catch (Exception e) {
                toast("保存失败：" + e.getMessage());
                return;
            }
            agent.relaunch();
            toast("已保存");
            done.run();
            refreshConfig(3);
        }, app.settings::refreshSettings);
    }
}
