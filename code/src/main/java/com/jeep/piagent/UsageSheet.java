package com.jeep.piagent;

import android.app.Dialog;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 用量 (the top bar's menu): how full this conversation's context window is, and what it has used so
 * far (tokens, cost), as pi counts them (the bridge's /context: pi's session stats).
 *
 * The context is one segmented bar of the window, a part per kind, each kind always in its own
 * colour, with a legend naming them.
 */
final class UsageSheet extends Part {
    /** The categorical palette (validated, light and dark), slots in a fixed order. */
    private static final int[] LIGHT = {0xFF2A78D6, 0xFFEB6834, 0xFF1BAF7A, 0xFFEDA100, 0xFFE87BA4, 0xFF008300, 0xFF6250D6, 0xFFE34948};
    private static final int[] DARK = {0xFF3987E5, 0xFFD95926, 0xFF199E70, 0xFFC98500, 0xFFD55181, 0xFF008300, 0xFF9085E9, 0xFFE66767};
    /** The kinds of what fills the context, each its own slot whatever else is there: a colour follows its kind. */
    private static final String[][] KINDS = {
            {"System prompt", "系统提示"}, {"System tools", "系统工具"}, {"MCP tools", "MCP 工具"},
            {"Custom agents", "自定义代理"}, {"Memory files", "记忆文件"}, {"Skills", "技能"},
            {"Messages", "消息"}, {"MCP server instructions", "MCP 服务说明"}};

    UsageSheet(MainActivity app) { super(app); }

    void show() {
        Dialog sheet = sheets.sheet("用量", null);
        LinearLayout box = sheets.content(sheet);

        label(box, "上下文");
        LinearLayout context = card(box);
        context.addView(waitingLine("正在读取…"));

        label(box, "本对话");
        LinearLayout stats = card(box);
        stats.addView(waitingLine("正在读取…"));

        sheet.show();

        Chat chat = app.current;
        bridge.post("/context", obj("chat", chat.key, "sessionId", chat.sessionId), (status, data) -> {
            context.removeAllViews();
            JSONObject c = status == 200 && data != null ? data.optJSONObject("context") : null;
            stats.removeAllViews();
            if (c == null) {
                context.addView(waitingLine("这个对话的 pi 还没在运行：发一条消息后就能看到"));
                stats.addView(waitingLine("同上"));
                return;
            }
            fillContext(context, c);
            if (c.optJSONObject("stats") != null) fillStats(stats, c.optJSONObject("stats"));
        });
    }

    // ---- 上下文 ----

    private void fillContext(LinearLayout card, JSONObject c) {
        long total = c.optLong("totalTokens"), max = Math.max(1, c.optLong("maxTokens"));
        int[] palette = Themes.dark(app) ? DARK : LIGHT;

        // The headline: the share of the window in use, and how much of how much.
        LinearLayout head = Ui.row(app);
        head.setGravity(Gravity.BOTTOM);
        TextView pct = Ui.text(app, Math.round(100.0 * total / max) + "%", 30, R.color.text);
        pct.setTypeface(Ui.MEDIUM);
        head.addView(pct);
        head.addView(new View(app), new LinearLayout.LayoutParams(0, 1, 1));
        TextView of = Ui.text(app, tokens(total) + " / " + tokens(max), 14, R.color.text2);
        of.setPadding(0, 0, 0, dp(5));
        head.addView(of);
        card.addView(head, Ui.fill());

        // The parts in use, each in its kind's colour; the buffer kept for compacting in grey; the rest free.
        List<long[]> parts = new ArrayList<>();
        List<String> names = new ArrayList<>();
        long buffer = 0;
        JSONArray cats = c.optJSONArray("categories");
        for (int i = 0; cats != null && i < cats.length(); i++) {
            JSONObject k = cats.optJSONObject(i);
            if (k == null || k.optBoolean("deferred")) continue; // not loaded: in no part of the window
            String name = k.optString("name");
            long t = k.optLong("tokens");
            if (name.equals("Free space")) continue;
            if (name.equals("Autocompact buffer")) { buffer = t; continue; }
            if (t <= 0) continue;
            parts.add(new long[]{t, slot(name)});
            names.add(name);
        }
        int bufferColor = Ui.color(app, R.color.borderStrong);
        Segments bar = new Segments(app, max, Ui.color(app, R.color.surface2));
        for (long[] p : parts) bar.add(p[0], palette[(int) p[1]]);
        if (buffer > 0) bar.add(buffer, bufferColor);
        bar.setContentDescription("上下文已用 " + tokens(total) + "，共 " + tokens(max));
        card.addView(bar, new LinearLayout.LayoutParams(-1, dp(10)));
        Ui.margins(bar, app, 0, 10, 0, 6);

        // The legend, in two columns (all of the sheet on one screen): each part's name and amount, in the bar's order.
        List<Object[]> items = new ArrayList<>();
        for (int i = 0; i < parts.size(); i++) items.add(new Object[]{palette[(int) parts.get(i)[1]], chinese(names.get(i)), parts.get(i)[0]});
        if (buffer > 0) items.add(new Object[]{bufferColor, "自动压缩预留", buffer});
        items.add(new Object[]{Ui.color(app, R.color.surface2), "空闲", Math.max(0, max - total - buffer)});
        int rows = (items.size() + 1) / 2;
        for (int r = 0; r < rows; r++) {
            LinearLayout line = Ui.row(app);
            for (int col = 0; col < 2; col++) {
                int i = col * rows + r; // down the first column, then the second: the bar's order
                LinearLayout cell = i < items.size()
                        ? legend((int) items.get(i)[0], (String) items.get(i)[1], (long) items.get(i)[2]) : Ui.row(app);
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, -2, 1);
                if (col == 0) lp.setMargins(0, 0, dp(14), 0);
                line.addView(cell, lp);
            }
            card.addView(line, Ui.fill());
        }
    }

    private static int slot(String name) {
        for (int i = 0; i < KINDS.length; i++) if (KINDS[i][0].equals(name)) return i;
        return 7; // anything else: the last slot, shared
    }

    private static String chinese(String name) {
        for (String[] k : KINDS) if (k[0].equals(name)) return k[1];
        return name;
    }

    private LinearLayout legend(int color, String name, long tokens) {
        LinearLayout row = Ui.row(app);
        row.setPadding(0, dp(4), 0, dp(4));
        View dot = new View(app);
        dot.setBackground(Ui.round(color, dp(3), dp(1), Ui.color(app, R.color.border)));
        row.addView(dot, new LinearLayout.LayoutParams(dp(10), dp(10)));
        TextView n = Ui.text(app, name, 13.5f, R.color.text);
        n.setPadding(dp(8), 0, dp(4), 0);
        n.setSingleLine(true);
        n.setEllipsize(TextUtils.TruncateAt.END);
        row.addView(n, Ui.weight());
        row.addView(Ui.text(app, tokens(tokens), 13.5f, R.color.text2));
        return row;
    }

    // ---- 本对话 ----

    /** What this conversation has used so far, as pi counts it (its session stats): tokens and cost. */
    private void fillStats(LinearLayout card, JSONObject stats) {
        JSONObject t = stats.optJSONObject("tokens");
        if (t == null) t = new JSONObject();
        statRow(card, "输入", tokens(t.optLong("input")));
        statRow(card, "输出", tokens(t.optLong("output")));
        statRow(card, "缓存读取", tokens(t.optLong("cacheRead")));
        if (t.optLong("cacheWrite") > 0) statRow(card, "缓存写入", tokens(t.optLong("cacheWrite")));
        double cost = stats.optDouble("cost", 0);
        statRow(card, "花费", cost <= 0 ? "—" : String.format(Locale.ROOT, cost < 0.01 ? "$%.4f" : "$%.2f", cost));
        statRow(card, "消息", stats.optInt("userMessages") + " 条提问 · " + stats.optInt("toolCalls") + " 次工具调用");
    }

    private void statRow(LinearLayout card, String name, String value) {
        LinearLayout row = Ui.row(app);
        row.setPadding(0, dp(5), 0, dp(5));
        row.addView(Ui.text(app, name, 14, R.color.text), Ui.weight());
        row.addView(Ui.text(app, value, 14, R.color.text2));
        card.addView(row, Ui.fill());
    }

    // ---- 共用 ----

    private void label(LinearLayout box, String text) {
        TextView t = Ui.text(app, text, 14, R.color.text2);
        t.setTypeface(Ui.MEDIUM);
        t.setPadding(dp(6), dp(box.getChildCount() > 0 ? 14 : 0), 0, dp(6));
        box.addView(t);
    }

    private LinearLayout card(LinearLayout box) {
        LinearLayout card = Ui.column(app);
        card.setPadding(dp(16), dp(12), dp(16), dp(13));
        card.setBackground(Ui.round(col(R.color.surface), dp(18), dp(1), col(R.color.border)));
        box.addView(card, Ui.fill());
        return card;
    }

    private TextView waitingLine(String text) {
        TextView t = Ui.text(app, text, 14, R.color.text2);
        t.setLineSpacing(0, 1.2f);
        return t;
    }

    /** 1,234 → 1.2k, 1,000,000 → 1m. */
    static String tokens(long n) {
        if (n < 1000) return String.valueOf(n);
        if (n < 1_000_000) return trim(n / 1000.0) + "k";
        return trim(n / 1_000_000.0) + "m";
    }

    private static String trim(double v) {
        String s = String.format(Locale.ROOT, "%.1f", v);
        return s.endsWith(".0") ? s.substring(0, s.length() - 2) : s;
    }

    /**
     * One whole split into parts, left to right: each part as wide as its share, a 2 dp gap of the surface
     * between parts, the ends round; what is left is the track. A part at zero draws nothing.
     */
    private static final class Segments extends View {
        private final long whole;
        private final int track, surface;
        private final List<long[]> parts = new ArrayList<>();
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path clip = new Path();
        private final RectF r = new RectF();

        Segments(Context c, long whole, int track) {
            super(c);
            this.whole = Math.max(1, whole);
            this.track = track;
            this.surface = Ui.color(c, R.color.surface);
        }

        void add(long amount, int color) {
            if (amount > 0) parts.add(new long[]{amount, color});
        }

        @Override protected void onDraw(Canvas canvas) {
            float w = getWidth(), h = getHeight(), gap = 2 * getResources().getDisplayMetrics().density;
            clip.reset();
            r.set(0, 0, w, h);
            clip.addRoundRect(r, h / 2, h / 2, Path.Direction.CW);
            canvas.save();
            canvas.clipPath(clip);
            paint.setColor(track);
            canvas.drawRect(r, paint);
            float x = 0, least = Math.max(gap * 1.5f, h / 3);
            for (int i = 0; i < parts.size(); i++) {
                float width = Math.max(least, w * parts.get(i)[0] / whole);
                float end = Math.min(w, x + width);
                paint.setColor((int) parts.get(i)[1]);
                canvas.drawRect(x, 0, Math.max(x, end - (end < w ? gap : 0)), h, paint);
                // The gap after each part shows the card's surface.
                if (end < w) { paint.setColor(surface); canvas.drawRect(end - gap, 0, end, h, paint); }
                x = end;
                if (x >= w) break;
            }
            canvas.restore();
        }
    }
}
