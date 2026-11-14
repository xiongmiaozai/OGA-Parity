package com.mcjb2vv.offline;

import android.Manifest;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
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
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.google.android.material.button.MaterialButton;
import com.google.android.material.button.MaterialButtonToggleGroup;
import com.google.android.material.card.MaterialCardView;
import com.google.android.material.chip.Chip;
import com.google.android.material.chip.ChipGroup;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.progressindicator.LinearProgressIndicator;
import com.google.android.material.switchmaterial.SwitchMaterial;
import com.google.android.material.appbar.MaterialToolbar;

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
 * 完全离线版主界面（v1.3，鸿蒙/HarmonyOS 风格改造，基于 Material 3 主题）。
 *
 * 视觉对照华为 EMUI / HarmonyOS 设计语言：
 *   - 翡翠绿主色 + 鸿蒙蓝（导入按钮），柔和中性灰背景
 *   - 大圆角卡片（22dp）、胶囊化按钮与分段控件、轻投影、舒适留白
 *   - 组件：MaterialToolbar / MaterialButton / SwitchMaterial / MaterialButtonToggleGroup
 *           / ChipGroup / MaterialCardView / LinearProgressIndicator / MaterialAlertDialog
 * 分区卡片结构（全程不联网）：
 *   顶栏 → 预设 → 选项（兼容/性能/线程/上限）→ 选择 → 进度(可取消) → 结果(校验+预览+导入+历史)
 * 转换内核是打包进 APK 的 Python core/（Chaquopy 运行时）。所有 Python 调用串行在
 * 同一后台线程，避免 Chaquopy 线程状态问题；S5 阶段的多线程并行由 Python 侧线程池负责。
 */
public class MainActivity extends AppCompatActivity {

    private static final int REQ_FILE = 2001;
    private static final int REQ_PERM = 2002;
    private static final String CHANNEL = "mcjb_offline";

    private final ExecutorService pyExec = Executors.newSingleThreadExecutor();

    private TextView statusText, stageText, logText;
    private LinearProgressIndicator progress;
    private MaterialButton pickBtn, importBtn, historyBtn, cancelBtn;
    private SwitchMaterial compatBox, perfBox;
    private ChipGroup presetGroup;
    private LinearLayout previewGrid, threadSeg, sizeSeg;
    private MaterialCardView resultCard;
    private ScrollView logScroll;
    private TextView resultTitle, resultMeta, fileInfoText;

    private volatile boolean initialized = false;
    private volatile boolean converting = false;
    private String pendingImportUri = null;
    private String selectedPreset = "vv_standard";
    private int selectedThreads = 0;   // 0=自动
    private int selectedMaxSize = 2048;
    private File rulesDir, dataDir, cacheZip;
    private String currentTid = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        float dp = getResources().getDisplayMetrics().density;

        dataDir = new File(getFilesDir(), "mcjb");
        rulesDir = new File(dataDir, "rules");
        cacheZip = new File(getCacheDir(), "input_pack.zip");
        copyAssetRules();

        // ---- 顶栏（MaterialToolbar + 离线徽标）----
        MaterialToolbar bar = new MaterialToolbar(this);
        bar.setTitle("MC 材质转换器 · 离线");
        bar.setTitleTextColor(Color.WHITE);
        bar.setMinimumHeight((int) (56 * dp));
        bar.setBackgroundColor(Color.parseColor("#00713A"));
        int barPad = (int) (12 * dp);
        bar.setPadding(barPad, barPad, barPad, barPad);

        // ---- 主体（可滚动）----
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        statusText = mkText("正在初始化本地转换引擎…", 14, "#333A45");
        statusText.setVisibility(View.GONE);   // 仅出错/完成时显示，平时不占位置
        body.addView(statusText);

        // ---- 预设 ----
        body.addView(sectionLabel("转换预设"));
        presetGroup = new ChipGroup(this);
        presetGroup.setSingleSelection(true);
        presetGroup.setSelectionRequired(false);
        ScrollView presetScroll = new ScrollView(this);
        presetScroll.setHorizontalScrollBarEnabled(true);
        presetScroll.addView(presetGroup);
        body.addView(presetScroll);

        // ---- 选项 ----
        body.addView(sectionLabel("转换选项"));
        // 性能模式：对大包体验提升明显，保持显眼
        perfBox = mkSwitch("性能模式（极速编码 + 贴图上限 256，更快更小，略损细节）");
        body.addView(perfBox);

        // 高级选项（默认折叠）：兼容模式 / 线程 / 贴图上限，普通用户无需调整
        final TextView advToggle = mkText("高级选项 ▸", 14, "#009A4E");
        advToggle.setTypeface(Typeface.DEFAULT_BOLD);
        advToggle.setPadding(0, (int) (10 * dp), 0, (int) (2 * dp));
        final LinearLayout adv = new LinearLayout(this);
        adv.setOrientation(LinearLayout.VERTICAL);
        adv.setVisibility(View.GONE);

        compatBox = mkSwitch("兼容模式（去 PBR / 引擎门槛 1.20.0，旧设备可用）");
        adv.addView(compatBox);

        LinearLayout threadWrap = new LinearLayout(this);
        threadWrap.setOrientation(LinearLayout.HORIZONTAL);
        threadWrap.setGravity(Gravity.CENTER_VERTICAL);
        threadWrap.addView(mkText("线程：", 13, "#444C58"));
        threadSeg = mkToggle(new String[]{"自动", "4", "8"}, 0, new TogglePick() {
            @Override public void onPick(int idx, String label) {
                selectedThreads = idx == 0 ? 0 : Integer.parseInt(label);
            }
        });
        threadWrap.addView(threadSeg);
        adv.addView(threadWrap);

        LinearLayout sizeWrap = new LinearLayout(this);
        sizeWrap.setOrientation(LinearLayout.HORIZONTAL);
        sizeWrap.setGravity(Gravity.CENTER_VERTICAL);
        sizeWrap.addView(mkText("贴图上限：", 13, "#444C58"));
        sizeSeg = mkToggle(new String[]{"512", "1024", "2048"}, 2, new TogglePick() {
            @Override public void onPick(int idx, String label) {
                selectedMaxSize = Integer.parseInt(label);
            }
        });
        sizeWrap.addView(sizeSeg);
        adv.addView(sizeWrap);

        final boolean[] advOpen = {false};
        advToggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                advOpen[0] = !advOpen[0];
                adv.setVisibility(advOpen[0] ? View.VISIBLE : View.GONE);
                advToggle.setText(advOpen[0] ? "高级选项 ▾" : "高级选项 ▸");
            }
        });
        body.addView(advToggle);
        body.addView(adv);

        // ---- 选择卡 ----
        body.addView(sectionLabel("选择资源包"));
        MaterialCardView pickCard = card(0xFFF2F8F4, dp);
        pickBtn = mkButton("点击选择 Java 资源包（.zip / .mcpack）", "#009A4E",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { pickFile(); }
                });
        pickBtn.setEnabled(false);
        fileInfoText = mkText(getString(R.string.pick_hint), 12, "#6B7686");
        pickCard.addView(pickBtn);
        pickCard.addView(fileInfoText);
        body.addView(pickCard);

        // ---- 进度 ----
        progress = new LinearProgressIndicator(this);
        progress.setIndeterminate(false);
        progress.setMax(100);
        progress.setTrackThickness((int) (6 * dp));
        progress.setTrackCornerRadius((int) (3 * dp));
        progress.setIndicatorColor(Color.parseColor("#009A4E"));
        progress.setTrackColor(Color.parseColor("#DDE8E1"));
        progress.setProgressCompat(0, false);
        LinearLayout.LayoutParams pLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        pLp.topMargin = (int) (14 * dp);
        body.addView(progress, pLp);

        stageText = mkText("准备就绪", 13, "#5A6472");
        LinearLayout.LayoutParams sLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sLp.topMargin = (int) (6 * dp);
        body.addView(stageText, sLp);

        cancelBtn = mkButton("取消转换", "#D94838", new View.OnClickListener() {
            @Override public void onClick(View v) { doCancel(); }
        });
        cancelBtn.setVisibility(View.GONE);
        body.addView(cancelBtn);

        // ---- 结果卡 ----
        resultCard = card(0xFFF0F6F2, dp);
        resultCard.setVisibility(View.GONE);
        resultTitle = mkText("", 15, "#00713A");
        resultTitle.setTypeface(Typeface.DEFAULT_BOLD);
        resultMeta = mkText("", 12, "#5A6472");
        previewGrid = new LinearLayout(this);
        previewGrid.setOrientation(LinearLayout.HORIZONTAL);
        previewGrid.setPadding(0, (int) (8 * dp), 0, (int) (8 * dp));
        resultCard.addView(resultTitle);
        resultCard.addView(resultMeta);
        resultCard.addView(previewGrid);
        importBtn = mkButton("用 Minecraft 基岩版打开 .mcpack", "#0A59F7",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { openImport(); }
                });
        importBtn.setVisibility(View.GONE);
        resultCard.addView(importBtn);
        historyBtn = mkButton("查看转换历史", "#6B7686", new View.OnClickListener() {
            @Override public void onClick(View v) { showHistory(); }
        });
        resultCard.addView(historyBtn);
        body.addView(resultCard);

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
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERM);
        }
        if (Build.VERSION.SDK_INT <= 28
                && ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE)
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
        TextView t = mkText(s, 13, "#009A4E");
        t.setTypeface(Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, (int) (14 * dp), 0, (int) (4 * dp));
        t.setLayoutParams(lp);
        return t;
    }

    private SwitchMaterial mkSwitch(String text) {
        float dp = getResources().getDisplayMetrics().density;
        SwitchMaterial sw = new SwitchMaterial(this);
        sw.setText(text);
        sw.setTextSize(13);
        sw.setChecked(false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (6 * dp);
        sw.setLayoutParams(lp);
        return sw;
    }

    private MaterialButton mkButton(String text, String bg, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        MaterialButton b = new MaterialButton(this);
        b.setText(text);
        b.setTextColor(Color.WHITE);
        b.setTextSize(15);
        b.setBackgroundTintList(ColorStateList.valueOf(Color.parseColor(bg)));
        b.setCornerRadius((int) (100 * dp));
        b.setPadding((int) (20 * dp), (int) (12 * dp), (int) (20 * dp), (int) (12 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (10 * dp);
        b.setLayoutParams(lp);
        b.setOnClickListener(l);
        return b;
    }

    private MaterialCardView card(int bg, float dp) {
        MaterialCardView c = new MaterialCardView(this);
        c.setCardBackgroundColor(ColorStateList.valueOf(bg));
        c.setRadius(22 * dp);
        c.setCardElevation(4 * dp);
        c.setContentPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (14 * dp));
        c.setStrokeColor(ColorStateList.valueOf(Color.parseColor("#E2E8E6")));
        c.setStrokeWidth((int) (1 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * dp);
        c.setLayoutParams(lp);
        return c;
    }

    /** M3 分段选择器：MaterialButtonToggleGroup（互斥、选中高亮）。 */
    private interface TogglePick { void onPick(int idx, String label); }

    private LinearLayout mkToggle(String[] labels, int defIdx, final TogglePick pick) {
        float dp = getResources().getDisplayMetrics().density;
        MaterialButtonToggleGroup g = new MaterialButtonToggleGroup(this);
        g.setSingleSelection(true);
        final int[] defId = {-1};
        ColorStateList bgOn = ColorStateList.valueOf(Color.parseColor("#009A4E"));
        ColorStateList bgOff = ColorStateList.valueOf(Color.parseColor("#E8EEF0"));
        ColorStateList txOn = ColorStateList.valueOf(Color.WHITE);
        ColorStateList txOff = ColorStateList.valueOf(Color.parseColor("#33414B"));
        for (int i = 0; i < labels.length; i++) {
            final MaterialButton b = new MaterialButton(this,
                    null, com.google.android.material.R.style.Widget_Material3_Button_OutlinedButton);
            final int idx = i;
            b.setId(View.generateViewId());
            b.setText(labels[i]);
            b.setTextSize(13);
            b.setCornerRadius((int) (100 * dp));
            b.setTag(labels[i]);
            b.setBackgroundTintList(i == defIdx ? bgOn : bgOff);
            b.setTextColor(i == defIdx ? txOn : txOff);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.rightMargin = (int) (6 * dp);
            b.setLayoutParams(lp);
            g.addView(b);
            if (i == defIdx) defId[0] = b.getId();
        }
        g.check(defId[0]);
        g.addOnButtonCheckedListener(new MaterialButtonToggleGroup.OnButtonCheckedListener() {
            @Override public void onButtonChecked(MaterialButtonToggleGroup group,
                                                  int checkedId, boolean isChecked) {
                if (!isChecked) return;
                for (int i = 0; i < group.getChildCount(); i++) {
                    MaterialButton b = (MaterialButton) group.getChildAt(i);
                    boolean on = b.getId() == checkedId;
                    b.setBackgroundTintList(on ? bgOn : bgOff);
                    b.setTextColor(on ? txOn : txOff);
                }
                Object tag = group.findViewById(checkedId).getTag();
                if (tag != null) pick.onPick(group.indexOfChild(group.findViewById(checkedId)), tag.toString());
            }
        });
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.HORIZONTAL);
        wrap.addView(g);
        return wrap;
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
                    ui(() -> {
                        statusText.setText("Python 运行时启动失败：" + app.getInitError());
                        statusText.setVisibility(View.VISIBLE);
                    });
                    return;
                }
                try {
                    PyObject mod = Python.getInstance().getModule("mcjb_app");
                    mod.callAttr("init", dataDir.getAbsolutePath(), rulesDir.getAbsolutePath());
                    final String ver = mod.callAttr("rules_version").toJava(String.class);
                    initialized = true;
                    final String presetsJson = mod.callAttr("presets").toJava(String.class);
                    ui(() -> {
                        pickBtn.setEnabled(true);
                        buildPresets(presetsJson);
                    });
                } catch (Throwable t) {
                    ui(() -> statusText.setText("初始化失败：" + t));
                }
            }
        });
    }

    private void buildPresets(String json) {
        presetGroup.removeAllViews();
        try {
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject p = arr.getJSONObject(i);
                final String id = p.optString("id", "vv_standard");
                final String name = p.optString("name", id);
                final Chip chip = new Chip(this,
                        null, com.google.android.material.R.style.Widget_Material3_Chip_Filter);
                chip.setText(name);
                chip.setCheckable(true);
                chip.setChecked(id.equals(selectedPreset));
                chip.setTag(id);
                ColorStateList cOn = ColorStateList.valueOf(Color.parseColor("#009A4E"));
                ColorStateList cOff = ColorStateList.valueOf(Color.parseColor("#E8EEF0"));
                ColorStateList tOn = ColorStateList.valueOf(Color.WHITE);
                ColorStateList tOff = ColorStateList.valueOf(Color.parseColor("#33414B"));
                chip.setChipBackgroundColor(i == 0 ? cOn : cOff);
                chip.setTextColor(i == 0 ? tOn : tOff);
                chip.setOnCheckedChangeListener(new android.widget.CompoundButton.OnCheckedChangeListener() {
                    @Override public void onCheckedChanged(android.widget.CompoundButton b, boolean checked) {
                        if (checked) {
                            selectedPreset = id;
                            for (int k = 0; k < presetGroup.getChildCount(); k++) {
                                Chip c = (Chip) presetGroup.getChildAt(k);
                                boolean o = c == chip;
                                c.setChipBackgroundColor(o ? cOn : cOff);
                                c.setTextColor(o ? tOn : tOff);
                            }
                            appendLog("预设：" + name);
                        }
                    }
                });
                presetGroup.addView(chip);
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
        progress.setProgressCompat(0, false);
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
                    ui(() -> stageText.setText("转换中"));
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
                            progress.setProgressCompat(pct, true);
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
                statusText.setVisibility(View.VISIBLE);
                return;
            }
            progress.setProgressCompat(100, true);
            File out = new File(outPath);
            resultCard.setVisibility(View.VISIBLE);
            resultTitle.setText("✅ 转换完成 · 校验通过");
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
        new MaterialAlertDialogBuilder(this)
                .setTitle(getString(R.string.history_title) + "（最近 " + rows.length() + " 条）")
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
            new MaterialAlertDialogBuilder(this)
                    .setTitle("无法直接打开")
                    .setMessage("请到系统「下载」目录找到 .mcpack 文件，点击后选择 Minecraft 基岩版导入。\n\n" + e)
                    .setPositiveButton("知道了", null).show();
        }
    }

    private void notifyDone(String name, Uri uri) {
        try {
            android.app.NotificationManager nm =
                    (android.app.NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(new android.app.NotificationChannel(CHANNEL,
                        "转换完成", android.app.NotificationManager.IMPORTANCE_HIGH));
            }
            Intent open = new Intent(Intent.ACTION_VIEW);
            open.setDataAndType(uri, "application/octet-stream");
            open.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            int flags = android.app.PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= android.app.PendingIntent.FLAG_IMMUTABLE;
            android.app.PendingIntent pi = android.app.PendingIntent.getActivity(this, 1, open, flags);
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
        if (logText == null) return;   // 运行日志 UI 已移除
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
