package com.mcjb2vv.offline;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.CheckBox;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 完全离线版主界面（v1.2 重做）。
 *
 * 分区卡片设计，全程不联网：
 *   顶栏 → 预设 → 选项（兼容/性能/线程/上限）→ 选择 → 进度(可取消) → 结果(校验+预览+导入+历史)
 * 转换内核是打包进 APK 的 Python core/（Chaquopy 运行时）。所有 Python 调用串行在
 * 同一后台线程，避免 Chaquopy 线程状态问题；S5 阶段的多线程并行由 Python 侧线程池负责。
 */
public class MainActivity extends Activity {

    private static final int REQ_FILE = 2001;
    private static final int REQ_PERM = 2002;
    private static final String CHANNEL = "mcjb_offline";

    private final ExecutorService pyExec = Executors.newSingleThreadExecutor();

    private TextView statusText, stageText, logText;
    private ProgressBar progress;
    private TextView pickBtn, importBtn, historyBtn, cancelBtn, fileInfoText;
    private CheckBox compatBox, perfBox;
    private LinearLayout presetRow, previewGrid, resultCard, sizeSeg, threadSeg;
    private ScrollView logScroll;
    private TextView resultTitle, resultMeta;

    private volatile boolean initialized = false;
    private volatile boolean converting = false;
    private String pendingImportUri = null;
    private String selectedPreset = "vv_standard";
    private int selectedThreads = 0;   // 0=自动
    private int selectedMaxSize = 2048;
    private File rulesDir, dataDir, cacheZip;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        float dp = getResources().getDisplayMetrics().density;
        int pad = (int) (12 * dp);

        dataDir = new File(getFilesDir(), "mcjb");
        rulesDir = new File(dataDir, "rules");
        cacheZip = new File(getCacheDir(), "input_pack.zip");
        copyAssetRules();

        // ---- 顶栏 ----
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(Color.parseColor("#1B3A2B"));
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(pad, (int) (12 * dp), pad, (int) (12 * dp));
        TextView title = new TextView(this);
        title.setText("MC 材质转换器 · 离线");
        title.setTextColor(Color.WHITE);
        title.setTextSize(16);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        bar.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView badge = new TextView(this);
        badge.setText("100% 本地 · 无需联网");
        badge.setTextColor(Color.parseColor("#A8DCC0"));
        badge.setTextSize(11);
        bar.addView(badge);

        // ---- 主体（可滚动）----
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        statusText = mkText("正在初始化本地转换引擎…", 14, "#333A45");
        body.addView(statusText);

        // ---- 预设 ----
        body.addView(sectionLabel("转换预设"));
        presetRow = new LinearLayout(this);
        presetRow.setOrientation(LinearLayout.HORIZONTAL);
        HorizontalScrollView hsv = new HorizontalScrollView(this);
        hsv.setHorizontalScrollBarEnabled(false);
        hsv.addView(presetRow);
        body.addView(hsv);

        // ---- 选项 ----
        body.addView(sectionLabel("转换选项"));
        compatBox = mkCheck("兼容模式（去 PBR / 引擎门槛 1.20.0，旧设备可用）");
        perfBox = mkCheck("性能模式（极速编码 + 贴图上限 256，更快更小，略损细节）");
        body.addView(compatBox);
        body.addView(perfBox);

        // 线程分段
        LinearLayout threadWrap = new LinearLayout(this);
        threadWrap.setOrientation(LinearLayout.HORIZONTAL);
        threadWrap.setGravity(Gravity.CENTER_VERTICAL);
        threadWrap.addView(mkText("线程：", 13, "#444C58"));
        threadSeg = mkSegment(new String[]{"自动", "4", "8"}, 0, new SegPick() {
            @Override public void onPick(int idx, String label) {
                selectedThreads = idx == 0 ? 0 : Integer.parseInt(label);
            }
        });
        threadWrap.addView(threadSeg);
        body.addView(threadWrap);

        // 上限分段
        LinearLayout sizeWrap = new LinearLayout(this);
        sizeWrap.setOrientation(LinearLayout.HORIZONTAL);
        sizeWrap.setGravity(Gravity.CENTER_VERTICAL);
        sizeWrap.addView(mkText("贴图上限：", 13, "#444C58"));
        sizeSeg = mkSegment(new String[]{"512", "1024", "2048"}, 2, new SegPick() {
            @Override public void onPick(int idx, String label) {
                selectedMaxSize = Integer.parseInt(label);
            }
        });
        sizeWrap.addView(sizeSeg);
        body.addView(sizeWrap);

        // ---- 选择卡 ----
        body.addView(sectionLabel("选择资源包"));
        LinearLayout pickCard = card(0xFFF4F8F5);
        pickBtn = mkButton("点击选择 Java 资源包（.zip / .mcpack）", "#2E7D4F",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { pickFile(); }
                });
        pickBtn.setEnabled(false);
        fileInfoText = mkText("支持 Java 版材质包；转换全程在你的设备本地完成。", 12, "#6B7686");
        LinearLayout.LayoutParams fp = (LinearLayout.LayoutParams) pickBtn.getLayoutParams();
        fp.bottomMargin = (int) (8 * dp);
        pickCard.addView(pickBtn);
        pickCard.addView(fileInfoText);
        body.addView(pickCard);

        // ---- 进度卡 ----
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgress(0);
        LinearLayout.LayoutParams pLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (5 * dp));
        pLp.topMargin = (int) (14 * dp);
        body.addView(progress, pLp);

        stageText = mkText("准备就绪", 13, "#5A6472");
        LinearLayout.LayoutParams sLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sLp.topMargin = (int) (6 * dp);
        body.addView(stageText, sLp);

        cancelBtn = mkButton("取消转换", "#B0413E", new View.OnClickListener() {
            @Override public void onClick(View v) { doCancel(); }
        });
        cancelBtn.setVisibility(View.GONE);
        body.addView(cancelBtn);

        // ---- 结果卡 ----
        resultCard = card(0xFFF1F6F1);
        resultCard.setVisibility(View.GONE);
        resultTitle = mkText("", 15, "#1B3A2B");
        resultTitle.setTypeface(Typeface.DEFAULT_BOLD);
        resultMeta = mkText("", 12, "#5A6472");
        previewGrid = new LinearLayout(this);
        previewGrid.setOrientation(LinearLayout.HORIZONTAL);
        previewGrid.setPadding(0, (int) (8 * dp), 0, (int) (8 * dp));
        resultCard.addView(resultTitle);
        resultCard.addView(resultMeta);
        resultCard.addView(previewGrid);
        importBtn = mkButton("用 Minecraft 基岩版打开 .mcpack", "#3B6FD4",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { openImport(); }
                });
        importBtn.setVisibility(View.GONE);
        resultCard.addView(importBtn);
        historyBtn = mkButton("查看转换历史", "#5A6472", new View.OnClickListener() {
            @Override public void onClick(View v) { showHistory(); }
        });
        resultCard.addView(historyBtn);
        body.addView(resultCard);

        // ---- 日志 ----
        body.addView(sectionLabel("运行日志"));
        logText = mkText("", 11, "#7A8492");
        logText.setTypeface(Typeface.MONOSPACE);
        logScroll = new ScrollView(this);
        logScroll.addView(logText, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams logLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        logLp.topMargin = (int) (8 * dp);
        logLp.height = (int) (120 * dp);
        body.addView(logScroll, logLp);

        ScrollView rootScroll = new ScrollView(this);
        rootScroll.addView(body);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(rootScroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERM);
        }
        if (Build.VERSION.SDK_INT <= 28
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERM);
        }

        initPython();
    }

    // ---------------------------------------------------------------- 视图工具
    private TextView mkText(String s, int size, String color) {
        TextView t = new TextView(this);
        t.setText(s); t.setTextSize(size); t.setTextColor(Color.parseColor(color));
        return t;
    }

    private TextView sectionLabel(String s) {
        float dp = getResources().getDisplayMetrics().density;
        TextView t = mkText(s, 13, "#2E7D4F");
        t.setTypeface(Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, (int) (14 * dp), 0, (int) (4 * dp));
        t.setLayoutParams(lp);
        return t;
    }

    private CheckBox mkCheck(String text) {
        float dp = getResources().getDisplayMetrics().density;
        CheckBox c = new CheckBox(this);
        c.setText(text);
        c.setTextSize(13);
        c.setTextColor(Color.parseColor("#444C58"));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (6 * dp);
        c.setLayoutParams(lp);
        return c;
    }

    private TextView mkButton(String text, String bg, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        TextView t = new TextView(this);
        t.setText(text); t.setTextColor(Color.WHITE); t.setTextSize(15);
        t.setGravity(Gravity.CENTER);
        t.setBackgroundColor(Color.parseColor(bg));
        t.setPadding((int) (14 * dp), (int) (13 * dp), (int) (14 * dp), (int) (13 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (10 * dp);
        t.setLayoutParams(lp);
        t.setOnClickListener(l); t.setClickable(true); t.setFocusable(true);
        return t;
    }

    private LinearLayout card(int bg) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setBackgroundColor(bg);
        c.setPadding((int) (14 * dp), (int) (12 * dp), (int) (14 * dp), (int) (12 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * dp);
        c.setLayoutParams(lp);
        return c;
    }

    /** 分段选择器：一组互斥按钮，选中高亮。 */
    private interface SegPick { void onPick(int idx, String label); }

    private LinearLayout mkSegment(String[] labels, int defIdx, final SegPick pick) {
        float dp = getResources().getDisplayMetrics().density;
        final LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        final List<TextView> btns = new ArrayList<>();
        final int[] cur = {defIdx};
        for (int i = 0; i < labels.length; i++) {
            final int idx = i;
            TextView b = new TextView(this);
            b.setText(labels[i]);
            b.setTextSize(13);
            b.setGravity(Gravity.CENTER);
            b.setPadding((int) (14 * dp), (int) (6 * dp), (int) (14 * dp), (int) (6 * dp));
            b.setBackgroundColor(Color.parseColor(idx == defIdx ? "#2E7D4F" : "#DDE5DF"));
            b.setTextColor(Color.parseColor(idx == defIdx ? "#FFFFFF" : "#33414B"));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.rightMargin = (int) (6 * dp);
            b.setLayoutParams(lp);
            b.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    cur[0] = idx;
                    for (int j = 0; j < btns.size(); j++) {
                        boolean on = j == idx;
                        btns.get(j).setBackgroundColor(Color.parseColor(on ? "#2E7D4F" : "#DDE5DF"));
                        btns.get(j).setTextColor(Color.parseColor(on ? "#FFFFFF" : "#33414B"));
                    }
                    pick.onPick(idx, labels[idx]);
                }
            });
            btns.add(b);
            row.addView(b);
        }
        return row;
    }

    private void copyAssetRules() {
        rulesDir.mkdirs();
        for (String n : new String[]{"bundle.json", "id_map.json", "schemas.json"}) {
            File dst = new File(rulesDir, n);
            if (dst.exists()) continue;
            try (InputStream in = getAssets().open(n);
                 OutputStream out = new FileOutputStream(dst)) {
                byte[] buf = new byte[8192];
                int r;
                while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
            } catch (Exception e) {
                ui(() -> Toast.makeText(this, "规则复制失败：" + e.getMessage(),
                        Toast.LENGTH_LONG).show());
            }
        }
    }

    private void initPython() {
        pyExec.execute(new Runnable() {
            @Override public void run() {
                App app = (App) getApplication();
                if (!app.awaitPython(60000)) {
                    ui(() -> statusText.setText("Python 运行时启动失败：" + app.getInitError()));
                    return;
                }
                try {
                    PyObject mod = Python.getInstance().getModule("mcjb_app");
                    mod.callAttr("init", dataDir.getAbsolutePath(), rulesDir.getAbsolutePath());
                    final String ver = mod.callAttr("rules_version").toJava(String.class);
                    initialized = true;
                    final String presetsJson = mod.callAttr("presets").toJava(String.class);
                    ui(() -> {
                        statusText.setText("本地引擎就绪 · 规则包 " + ver);
                        pickBtn.setEnabled(true);
                        appendLog("init 完成 | 规则包 " + ver);
                        appendLog("转换全程在设备本地完成，未申请联网权限。");
                        buildPresets(presetsJson);
                    });
                } catch (Throwable t) {
                    ui(() -> statusText.setText("初始化失败：" + t));
                }
            }
        });
    }

    private void buildPresets(String json) {
        float dp = getResources().getDisplayMetrics().density;
        presetRow.removeAllViews();
        try {
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject p = arr.getJSONObject(i);
                final String id = p.optString("id", "vv_standard");
                final String name = p.optString("name", id);
                final TextView chip = new TextView(this);
                chip.setText(name);
                chip.setTextSize(12);
                chip.setGravity(Gravity.CENTER);
                chip.setPadding((int) (12 * dp), (int) (7 * dp), (int) (12 * dp), (int) (7 * dp));
                final boolean on = id.equals(selectedPreset);
                chip.setBackgroundColor(Color.parseColor(on ? "#2E7D4F" : "#E3EBE5"));
                chip.setTextColor(Color.parseColor(on ? "#FFFFFF" : "#33414B"));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
                lp.rightMargin = (int) (8 * dp);
                chip.setLayoutParams(lp);
                chip.setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View v) {
                        selectedPreset = id;
                        for (int k = 0; k < presetRow.getChildCount(); k++) {
                            TextView c = (TextView) presetRow.getChildAt(k);
                            boolean o = c == chip;
                            c.setBackgroundColor(Color.parseColor(o ? "#2E7D4F" : "#E3EBE5"));
                            c.setTextColor(Color.parseColor(o ? "#FFFFFF" : "#33414B"));
                        }
                        appendLog("预设：" + name);
                    }
                });
                presetRow.addView(chip);
            }
        } catch (Exception ignored) { }
    }

    private void pickFile() {
        if (converting) { Toast.makeText(this, "正在转换中，请稍候", Toast.LENGTH_SHORT).show(); return; }
        Intent i = new Intent(Intent.ACTION_GET_CONTENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                "application/zip", "application/octet-stream", "*/*"});
        try {
            startActivityForResult(Intent.createChooser(i, "选择 Java 资源包"), REQ_FILE);
        } catch (Exception e) {
            Toast.makeText(this, "无法打开文件选择器", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_FILE) return;
        if (res != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        try (InputStream in = getContentResolver().openInputStream(uri);
             OutputStream out = new FileOutputStream(cacheZip)) {
            byte[] buf = new byte[1 << 16];
            int r;
            while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
        } catch (Exception e) {
            Toast.makeText(this, "读取资源包失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            return;
        }
        ui(() -> fileInfoText.setText("已选择：" + cacheZip.length() / 1024 + " KB · 点击开始转换"));
        startConvert();
    }

    private void startConvert() {
        if (!initialized) { Toast.makeText(this, "引擎尚未就绪", Toast.LENGTH_SHORT).show(); return; }
        converting = true;
        pickBtn.setEnabled(false);
        compatBox.setEnabled(false);
        perfBox.setEnabled(false);
        cancelBtn.setVisibility(View.VISIBLE);
        resultCard.setVisibility(View.GONE);
        progress.setProgress(0);
        logText.setText("");
        appendLog("输入：" + cacheZip.length() / 1024 + " KB");

        final JSONObject opts = new JSONObject();
        try {
            opts.put("preset", selectedPreset);
            opts.put("legacy_compat", compatBox.isChecked());
            opts.put("fast_encode", perfBox.isChecked());
            if (perfBox.isChecked()) opts.put("max_size", 256);
            else opts.put("max_size", selectedMaxSize);
            opts.put("threads", selectedThreads);
        } catch (Exception ignored) { }

        pyExec.execute(new Runnable() {
            @Override public void run() {
                try {
                    PyObject mod = Python.getInstance().getModule("mcjb_app");
                    String tid = mod.callAttr("start", cacheZip.getAbsolutePath(),
                            opts.toString()).toJava(String.class);
                    currentTid = tid;
                    ui(() -> stageText.setText("转换中 · 任务 " + tid));
                    long deadline = System.currentTimeMillis() + 30 * 60 * 1000L;
                    while (System.currentTimeMillis() < deadline) {
                        String js = mod.callAttr("poll", tid).toJava(String.class);
                        final JSONObject s = new JSONObject(js);
                        if (s.optBoolean("terminal", false)) {
                            if ("SUCCESS".equals(s.optString("state"))) {
                                String rj = mod.callAttr("result", tid).toJava(String.class);
                                finishConvert(new JSONObject(rj));
                            } else {
                                finishConvert(s);
                            }
                            return;
                        }
                        final int pct = (int) Math.round(s.optDouble("progress", 0) * 100);
                        final String stage = s.optString("stage", "");
                        final String file = s.optString("current_file", "");
                        final String eta = s.optString("eta_text", "");
                        final String elapsed = s.optString("elapsed_text", "");
                        ui(() -> {
                            progress.setProgress(pct);
                            StringBuilder sb = new StringBuilder();
                            sb.append(pct).append("% · ").append(stage);
                            if (!file.isEmpty()) sb.append(" · ").append(file);
                            if (!elapsed.isEmpty()) sb.append(" · 已用 ").append(elapsed);
                            if (!eta.isEmpty()) {
                                if (eta.equals("计算中")) sb.append(" · 预估计算中");
                                else sb.append(" · 剩余 ").append(eta);
                            }
                            stageText.setText(sb.toString());
                        });
                        Thread.sleep(400);
                    }
                    ui(() -> {
                        converting = false;
                        pickBtn.setEnabled(true);
                        compatBox.setEnabled(true);
                        perfBox.setEnabled(true);
                        cancelBtn.setVisibility(View.GONE);
                        stageText.setText("转换超时");
                    });
                } catch (Throwable t) {
                    ui(() -> {
                        converting = false;
                        pickBtn.setEnabled(true);
                        compatBox.setEnabled(true);
                        perfBox.setEnabled(true);
                        cancelBtn.setVisibility(View.GONE);
                        stageText.setText("转换异常：" + t);
                        appendLog(String.valueOf(t));
                    });
                }
            }
        });
    }

    private void doCancel() {
        pyExec.execute(new Runnable() {
            @Override public void run() {
                try {
                    Python.getInstance().getModule("mcjb_app").callAttr("cancel", currentTid);
                } catch (Throwable ignored) { }
                ui(() -> {
                    converting = false;
                    pickBtn.setEnabled(true);
                    compatBox.setEnabled(true);
                    perfBox.setEnabled(true);
                    cancelBtn.setVisibility(View.GONE);
                    stageText.setText("已取消");
                });
            }
        });
    }

    private String currentTid = "";

    private void finishConvert(JSONObject s) {
        final String state = s.optString("state", "");
        final String err = s.optString("error", "");
        final String outPath = s.optString("output_path", "");
        ui(() -> {
            converting = false;
            pickBtn.setEnabled(true);
            compatBox.setEnabled(true);
            perfBox.setEnabled(true);
            cancelBtn.setVisibility(View.GONE);
            if (!"SUCCESS".equals(state)) {
                stageText.setText("转换未成功：" + state + (err.isEmpty() ? "" : " · " + err));
                statusText.setText("请检查输入包是否为有效 Java 资源包");
                return;
            }
            progress.setProgress(100);
            File out = new File(outPath);
            resultCard.setVisibility(View.VISIBLE);
            resultTitle.setText("✅ 转换完成 · 12 项校验通过");
            resultMeta.setText(out.getName() + "（" + out.length() / 1024 + " KB）· 已用 "
                    + s.optString("elapsed_text", ""));
            buildPreview(s.optJSONObject("previews"));
            Uri exported = exportPack(out);
            if (exported != null) {
                pendingImportUri = exported.toString();
                statusText.setText("产物已保存到下载目录：" + out.getName()
                        + "（" + out.length() / 1024 + " KB）");
                importBtn.setVisibility(View.VISIBLE);
                notifyDone(out.getName(), exported);
            } else {
                statusText.setText("转换完成，但导出失败：" + out.getAbsolutePath());
            }
        });
    }

    /** 把预览 base64（原图/color/normal/mer）渲染为缩略图网格。 */
    private void buildPreview(JSONObject previews) {
        float dp = getResources().getDisplayMetrics().density;
        previewGrid.removeAllViews();
        if (previews == null) return;
        String[] keys = {"source", "color", "normal", "mer"};
        String[] labels = {"原图", "色彩", "法线", "MER"};
        for (int i = 0; i < keys.length; i++) {
            String b64 = previews.optString(keys[i]);
            if (b64 == null || b64.isEmpty()) continue;
            try {
                if (b64.startsWith("data:")) b64 = b64.substring(b64.indexOf(",") + 1);
                byte[] raw = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
                Bitmap bm = BitmapFactory.decodeByteArray(raw, 0, raw.length);
                if (bm == null) continue;
                LinearLayout cell = new LinearLayout(this);
                cell.setOrientation(LinearLayout.VERTICAL);
                cell.setGravity(Gravity.CENTER_HORIZONTAL);
                cell.setPadding(0, 0, (int) (8 * dp), 0);
                ImageView iv = new ImageView(this);
                int sz = (int) (84 * dp);
                iv.setLayoutParams(new LinearLayout.LayoutParams(sz, sz));
                iv.setScaleType(ImageView.ScaleType.FIT_CENTER);
                iv.setImageBitmap(bm);
                TextView lb = mkText(labels[i], 11, "#5A6472");
                lb.setGravity(Gravity.CENTER);
                cell.addView(iv);
                cell.addView(lb);
                previewGrid.addView(cell);
            } catch (Exception ignored) { }
        }
    }

    private void showHistory() {
        pyExec.execute(new Runnable() {
            @Override public void run() {
                try {
                    String js = Python.getInstance().getModule("mcjb_app")
                            .callAttr("history", 0, 50).toJava(String.class);
                    final JSONArray rows = new JSONArray(js);
                    ui(() -> presentHistory(rows));
                } catch (Throwable t) {
                    ui(() -> Toast.makeText(MainActivity.this,
                            "读取历史失败：" + t, Toast.LENGTH_SHORT).show());
                }
            }
        });
    }

    private void presentHistory(JSONArray rows) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        if (rows.length() == 0) {
            list.addView(mkText("暂无转换记录", 13, "#5A6472"));
        }
        for (int i = 0; i < rows.length(); i++) {
            try {
                JSONObject r = rows.getJSONObject(i);
                TextView row = mkText(
                        r.optString("input_name", "?") + "  ·  "
                                + r.optString("status", "") + "  ·  "
                                + (r.optLong("duration_ms", 0) / 1000) + "s  ·  "
                                + r.optString("started_at", ""), 12, "#33414B");
                row.setPadding(0, (int) (6 * dp), 0, (int) (6 * dp));
                list.addView(row);
            } catch (Exception ignored) { }
        }
        new AlertDialog.Builder(this)
                .setTitle("转换历史（最近 " + rows.length() + " 条）")
                .setView(list)
                .setPositiveButton("关闭", null)
                .show();
    }

    private Uri exportPack(File src) {
        if (src == null || !src.exists()) return null;
        String name = src.getName();
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Downloads.DISPLAY_NAME, name);
                cv.put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream");
                cv.put(MediaStore.Downloads.IS_PENDING, 1);
                Uri uri = getContentResolver().insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                if (uri == null) return null;
                try (InputStream in = new java.io.FileInputStream(src);
                     OutputStream out = getContentResolver().openOutputStream(uri)) {
                    copy(in, out);
                }
                cv.clear();
                cv.put(MediaStore.Downloads.IS_PENDING, 0);
                getContentResolver().update(uri, cv, null, null);
                return uri;
            } else {
                File dir = Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_DOWNLOADS);
                dir.mkdirs();
                File dst = new File(dir, name);
                try (InputStream in = new java.io.FileInputStream(src);
                     OutputStream out = new FileOutputStream(dst)) {
                    copy(in, out);
                }
                return Uri.fromFile(dst);
            }
        } catch (Exception e) {
            return null;
        }
    }

    private static void copy(InputStream in, OutputStream out) throws Exception {
        byte[] buf = new byte[1 << 16];
        int r;
        while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
    }

    private void openImport() {
        if (pendingImportUri == null) return;
        try {
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(Uri.parse(pendingImportUri), "application/octet-stream");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            new AlertDialog.Builder(this)
                    .setTitle("无法直接打开")
                    .setMessage("请到系统「下载」目录找到 .mcpack 文件，点击后选择 Minecraft 基岩版导入。\n\n" + e)
                    .setPositiveButton("知道了", null).show();
        }
    }

    private void notifyDone(String name, Uri uri) {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(new NotificationChannel(CHANNEL,
                        "转换完成", NotificationManager.IMPORTANCE_HIGH));
            }
            Intent open = new Intent(Intent.ACTION_VIEW);
            open.setDataAndType(uri, "application/octet-stream");
            open.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(this, 1, open, flags);
            android.app.Notification.Builder b = Build.VERSION.SDK_INT >= 26
                    ? new android.app.Notification.Builder(this, CHANNEL)
                    : new android.app.Notification.Builder(this);
            b.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("材质包转换完成")
                    .setContentText(name + " · 点击导入 Minecraft")
                    .setContentIntent(pi).setAutoCancel(true);
            nm.notify(2, b.build());
        } catch (Exception ignored) { }
    }

    private void appendLog(String line) {
        logText.append(line + "\n");
        logScroll.post(() -> logScroll.fullScroll(View.FOCUS_DOWN));
    }

    private void ui(Runnable r) { runOnUiThread(r); }

    @Override
    protected void onDestroy() {
        pyExec.shutdownNow();
        super.onDestroy();
    }
}
