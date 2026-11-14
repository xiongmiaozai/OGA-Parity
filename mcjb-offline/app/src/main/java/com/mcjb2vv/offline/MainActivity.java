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
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.progressindicator.LinearProgressIndicator;
import com.google.android.material.switchmaterial.SwitchMaterial;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * MC 材质转换器 · 离线版（Android）。
 * 视觉对照华为 EMUI / HarmonyOS 设计语言：
 *   - 翡翠绿主色 + 鸿蒙蓝（导入按钮），柔和中性灰背景
 *   - 大圆角卡片（22dp）、胶囊化按钮与分段控件、轻投影、舒适留白
 * 底部三标签导航（全程不联网）：
 *   转换 → 预设 / 选择 / 进度(可取消) / 结果(校验+预览+导入)
 *   设置 → 转换选项（性能/兼容/线程/上限）+ 关于
 *   资源库 → 导入的 Java 资源包 / 导出的基岩版 .mcpack（查看/下载/删除）
 * 转换内核是打包进 APK 的 Python core/（Chaquopy 运行时）。所有 Python 调用串行在
 * 同一后台线程，避免 Chaquopy 线程状态问题；S5 阶段的多线程并行由 Python 侧线程池负责。
 */
public class MainActivity extends AppCompatActivity {

    private static final int REQ_FILE = 2001;
    private static final int REQ_PERM = 2002;
    private static final int REQ_SAVE = 2003;
    private static final int REQ_LIB_EXPORT = 3001;   // 资源库条目 SAF 导出
    private static final int TAB_CONVERT = 0;
    private static final int TAB_SETTINGS = 1;
    private static final int TAB_LIBRARY = 2;
    private static final String CHANNEL = "mcjb_offline";

    private final ExecutorService pyExec = Executors.newSingleThreadExecutor();

    private MaterialToolbar bar;
    private TextView statusText, stageText, logText;
    private LinearProgressIndicator progress;
    private MaterialButton pickBtn, importBtn, historyBtn, cancelBtn, saveBtn, saveAsBtn;
    private SwitchMaterial compatBox, perfBox;
    private LinearLayout presetGroup;   // 横向 LinearLayout 承载预设胶囊（原 ChipGroup，已改自绘）
    private LinearLayout previewGrid, threadSeg, sizeSeg, body;
    private LinearLayout resultCard;
    private ScrollView logScroll;
    private TextView resultTitle, resultMeta, fileInfoText;

    // 三页内容容器
    private FrameLayout contentFrame;
    private ScrollView convertScroll, settingsScroll, libraryScroll;
    private LinearLayout settingsBody, libraryBody;
    private TextView[] navLabels = new TextView[3];
    private int currentTab = TAB_CONVERT;

    // 资源库：分组维度 / 排序方向 / 类型过滤
    private static final int GROUP_TIME = 0, GROUP_TYPE = 1, GROUP_BATCH = 2;
    private static final int FILTER_ALL = 0, FILTER_JAVA = 1, FILTER_BEDROCK = 2;

    private LibraryStore libraryStore;
    private LinearLayout libraryList;
    private int libGroup = GROUP_TYPE;      // 默认按类型（Java / 基岩版）分组
    private boolean libNewestFirst = true;  // 默认最新在前
    private int libFilter = FILTER_ALL;     // 默认显示全部
    private String pendingExportPath = null;
    private String pendingExportName = null;

    private volatile boolean initialized = false;
    private volatile boolean converting = false;
    private String pendingImportUri = null;
    private String pendingOutPath = null;   // 转换产物本地路径，供「保存」使用
    private String selectedPreset = "vv_standard";
    private int selectedThreads = 0;   // 0=自动
    private int selectedMaxSize = 2048;
    private File rulesDir, dataDir, cacheZip;
    private String currentTid = "";
    private String lastInputName = "input_pack.zip";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        float dp = getResources().getDisplayMetrics().density;

        dataDir = new File(getFilesDir(), "mcjb");
        rulesDir = new File(dataDir, "rules");
        cacheZip = new File(getCacheDir(), "input_pack.zip");
        libraryStore = new LibraryStore(this);
        copyAssetRules();

        // ---- 顶栏 ----
        bar = new MaterialToolbar(this);
        bar.setTitle("MC 材质转换器 · 离线");
        bar.setTitleTextColor(Color.WHITE);
        bar.setMinimumHeight((int) (56 * dp));
        bar.setBackgroundColor(Color.parseColor("#00713A"));
        int barPad = (int) (12 * dp);
        bar.setPadding(barPad, barPad, barPad, barPad);

        // ---- 三个页面 ----
        buildConvertPage(dp);
        buildSettingsPage(dp);
        buildLibraryPage(dp);

        contentFrame = new FrameLayout(this);
        FrameLayout.LayoutParams cfLp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        contentFrame.setLayoutParams(cfLp);
        contentFrame.addView(convertScroll);
        contentFrame.addView(settingsScroll);
        contentFrame.addView(libraryScroll);
        settingsScroll.setVisibility(View.GONE);
        libraryScroll.setVisibility(View.GONE);

        // ---- 底部三标签导航 ----
        LinearLayout nav = buildBottomNav(dp);

        // ---- 根布局 ----
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(contentFrame, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(nav, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
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

    // ============================================================ 转换页
    private void buildConvertPage(float dp) {
        body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        statusText = mkText("正在初始化本地转换引擎…", 14, "#333A45");
        statusText.setVisibility(View.GONE);
        addBody(statusText);

        // 预设（自绘胶囊，横向滚动）
        body.addView(sectionLabel("转换预设"));
        presetGroup = new LinearLayout(this);
        presetGroup.setOrientation(LinearLayout.HORIZONTAL);
        HorizontalScrollView presetScroll = new HorizontalScrollView(this);
        presetScroll.setHorizontalScrollBarEnabled(false);
        presetScroll.addView(presetGroup);
        addBody(presetScroll);

        // 选择卡
        body.addView(sectionLabel("选择资源包"));
        LinearLayout pickCard = card(0xFFF2F8F4, dp);
        pickBtn = mkButton("点击选择 Java 资源包（.zip / .mcpack）", "#009A4E",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { pickFile(); }
                });
        pickBtn.setEnabled(false);
        fileInfoText = mkText("选完文件后点上面的按钮即可开始转换", 12, "#6B7686");
        pickCard.addView(pickBtn);
        pickCard.addView(fileInfoText);
        body.addView(pickCard);

        // 进度
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

        // 结果卡
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
        saveBtn = mkButton("保存到下载目录", "#009A4E",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { doSaveToDownloads(); }
                });
        saveBtn.setVisibility(View.GONE);
        resultCard.addView(saveBtn);
        saveAsBtn = mkButton("另存为…（选择保存位置）", "#0A59F7",
                new View.OnClickListener() {
                    @Override public void onClick(View v) { doSaveAs(); }
                });
        saveAsBtn.setVisibility(View.GONE);
        resultCard.addView(saveAsBtn);
        historyBtn = mkButton("前往资源库（导入 / 导出存档）", "#6B7686", new View.OnClickListener() {
            @Override public void onClick(View v) { selectTab(TAB_LIBRARY); }
        });
        resultCard.addView(historyBtn);
        body.addView(resultCard);

        convertScroll = new ScrollView(this);
        convertScroll.addView(body);
    }

    // ============================================================ 设置页
    private void buildSettingsPage(float dp) {
        settingsBody = new LinearLayout(this);
        settingsBody.setOrientation(LinearLayout.VERTICAL);
        settingsBody.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        settingsBody.addView(sectionLabel("转换选项"));

        perfBox = mkSwitch("性能模式（极速编码 + 贴图上限 256，更快更小，略损细节）");
        stack(settingsBody, perfBox, 8);

        compatBox = mkSwitch("兼容模式（去 PBR / 引擎门槛 1.20.0，旧设备可用）");
        stack(settingsBody, compatBox, 4);

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
        stack(settingsBody, threadWrap, 8);

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
        stack(settingsBody, sizeWrap, 8);

        // 关于
        settingsBody.addView(sectionLabel("关于"));
        LinearLayout about = card(0xFFF0F6F2, dp);
        TextView aboutText = mkText(
                "MC 材质转换器 · 离线版\n" +
                "版本 " + appVersion() + "\n" +
                "全部转换在设备本地完成，无需联网。\n" +
                "导入的 Java 资源包与导出的基岩版 .mcpack 均可在「资源库」长期查看与下载。",
                13, "#33414B");
        aboutText.setLineSpacing(2 * dp, 1.05f);
        about.addView(aboutText);
        stack(settingsBody, about, 8);

        settingsScroll = new ScrollView(this);
        settingsScroll.addView(settingsBody);
    }

    private String appVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "1.4.5";
        }
    }

    // ============================================================ 资源库页
    private void buildLibraryPage(float dp) {
        libraryBody = new LinearLayout(this);
        libraryBody.setOrientation(LinearLayout.VERTICAL);
        libraryBody.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        // 分类工具条：分组维度 / 排序方向 / 类型过滤
        LinearLayout tools = card(0xFFFFFFFF, dp);
        TextView tt = mkText("分类与排列", 13, "#00713A");
        tt.setTypeface(null, Typeface.BOLD);
        tools.addView(tt);

        tools.addView(toolLabel("分组方式", dp));
        tools.addView(mkToggle(new String[]{"按时间", "按类型", "按批次"}, libGroup,
                new TogglePick() {
                    @Override public void onPick(int idx, String label) {
                        libGroup = idx; refreshLibrary();
                    }
                }));
        tools.addView(toolLabel("排序", dp));
        tools.addView(mkToggle(new String[]{"最新在前", "最早在前"}, 0,
                new TogglePick() {
                    @Override public void onPick(int idx, String label) {
                        libNewestFirst = (idx == 0); refreshLibrary();
                    }
                }));
        tools.addView(toolLabel("显示", dp));
        tools.addView(mkToggle(new String[]{"全部", "仅 Java", "仅基岩版"}, libFilter,
                new TogglePick() {
                    @Override public void onPick(int idx, String label) {
                        libFilter = idx; refreshLibrary();
                    }
                }));
        libraryBody.addView(tools);

        libraryList = new LinearLayout(this);
        libraryList.setOrientation(LinearLayout.VERTICAL);
        libraryBody.addView(libraryList);

        libraryScroll = new ScrollView(this);
        libraryScroll.addView(libraryBody);
    }

    private TextView toolLabel(String s, float dp) {
        TextView t = mkText(s, 11, "#6B7686");
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (10 * dp);
        t.setLayoutParams(lp);
        return t;
    }

    private final Map<String, Bitmap> iconCache = new java.util.HashMap<>();

    /** 按当前「分组 / 排序 / 过滤」设置重新渲染资源库列表。 */
    private void refreshLibrary() {
        if (libraryList == null) return;
        libraryList.removeAllViews();
        float dp = getResources().getDisplayMetrics().density;

        List<LibraryStore.Item> all = libraryStore.all();   // 新 → 旧
        List<LibraryStore.Item> items = new ArrayList<>();
        for (LibraryStore.Item it : all) {
            boolean isJava = LibraryStore.KIND_IMPORT.equals(it.kind);
            if (libFilter == FILTER_JAVA && !isJava) continue;
            if (libFilter == FILTER_BEDROCK && isJava) continue;
            items.add(it);
        }
        if (!libNewestFirst) Collections.reverse(items);

        if (items.isEmpty()) {
            libraryList.addView(emptyHint("没有符合条件的记录，去「转换」页转换后会自动存档"));
            return;
        }

        if (libGroup == GROUP_TYPE) {
            List<LibraryStore.Item> java = new ArrayList<>(), be = new ArrayList<>();
            for (LibraryStore.Item it : items)
                (LibraryStore.KIND_IMPORT.equals(it.kind) ? java : be).add(it);
            libraryList.addView(sectionLabel("导入的 Java 资源包 · " + java.size()));
            addLibItems(java);
            libraryList.addView(sectionLabel("导出的基岩版 .mcpack · " + be.size()));
            addLibItems(be);

        } else if (libGroup == GROUP_TIME) {
            LinkedHashMap<String, List<LibraryStore.Item>> groups = new LinkedHashMap<>();
            for (LibraryStore.Item it : items) {
                String key = dayLabel(it.createdAt);
                if (!groups.containsKey(key)) groups.put(key, new ArrayList<LibraryStore.Item>());
                groups.get(key).add(it);
            }
            for (Map.Entry<String, List<LibraryStore.Item>> e : groups.entrySet()) {
                libraryList.addView(sectionLabel(e.getKey() + " · " + e.getValue().size()));
                addLibItems(e.getValue());
            }

        } else {   // 按批次：一次转换的输入包 + 产物归为一组
            LinkedHashMap<String, List<LibraryStore.Item>> groups = new LinkedHashMap<>();
            for (LibraryStore.Item it : items) {
                String key = (it.batchId == null || it.batchId.isEmpty())
                        ? "single_" + it.id : it.batchId;
                if (!groups.containsKey(key)) groups.put(key, new ArrayList<LibraryStore.Item>());
                groups.get(key).add(it);
            }
            int n = 0;
            for (Map.Entry<String, List<LibraryStore.Item>> e : groups.entrySet()) {
                n++;
                List<LibraryStore.Item> g = e.getValue();
                // 组内 Java 输入在前、基岩版产物在后
                Collections.sort(g, new Comparator<LibraryStore.Item>() {
                    @Override public int compare(LibraryStore.Item a, LibraryStore.Item b) {
                        int ka = LibraryStore.KIND_IMPORT.equals(a.kind) ? 0 : 1;
                        int kb = LibraryStore.KIND_IMPORT.equals(b.kind) ? 0 : 1;
                        return ka - kb;
                    }
                });
                String input = "";
                long ts = 0;
                for (LibraryStore.Item it : g) {
                    if (it.inputName != null && !it.inputName.isEmpty()) input = it.inputName;
                    if (it.createdAt > ts) ts = it.createdAt;
                }
                libraryList.addView(sectionLabel("批次 " + n + " · " + fmtTime(ts)
                        + (input.isEmpty() ? "" : " · 输入：" + input)));
                addLibItems(g);
            }
        }
    }

    private void addLibItems(List<LibraryStore.Item> list) {
        for (LibraryStore.Item it : list) libraryList.addView(buildLibCard(it));
    }

    private String dayLabel(long ms) {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd", Locale.CHINA);
        String today = f.format(new Date());
        String d = f.format(new Date(ms));
        if (d.equals(today)) return "今天";
        if (d.equals(f.format(new Date(System.currentTimeMillis() - 24 * 3600 * 1000L)))) return "昨天";
        return d;
    }

    /** 资源包图标：优先读取包内 pack_icon.png / pack.png，缺失则用彩色字母占位。 */
    private View makePackIcon(LibraryStore.Item it, float dp) {
        int sz = (int) (44 * dp);
        boolean isJava = LibraryStore.KIND_IMPORT.equals(it.kind);
        Bitmap bm = iconCache.get(it.stored);
        if (bm == null) {
            byte[] png = libraryStore.iconOf(it);
            if (png != null) {
                try {
                    // 先取尺寸，大图按目标尺寸降采样，避免 decodeByteArray 直接 OOM
                    BitmapFactory.Options bounds = new BitmapFactory.Options();
                    bounds.inJustDecodeBounds = true;
                    BitmapFactory.decodeByteArray(png, 0, png.length, bounds);
                    int sample = 1;
                    int maxDim = Math.max(bounds.outWidth, bounds.outHeight);
                    int target = (int) (192 * dp);
                    while (maxDim / sample > target) sample *= 2;
                    BitmapFactory.Options opt = new BitmapFactory.Options();
                    opt.inSampleSize = sample;
                    bm = BitmapFactory.decodeByteArray(png, 0, png.length, opt);
                } catch (Throwable ignored) { }
            }
            if (bm != null) iconCache.put(it.stored, bm);
        }
        if (bm != null) {
            ImageView iv = new ImageView(this);
            iv.setLayoutParams(new LinearLayout.LayoutParams(sz, sz));
            iv.setScaleType(ImageView.ScaleType.FIT_CENTER);
            iv.setImageBitmap(bm);
            return iv;
        }
        TextView t = new TextView(this);
        t.setLayoutParams(new LinearLayout.LayoutParams(sz, sz));
        t.setGravity(Gravity.CENTER);
        t.setText(isJava ? "J" : "B");
        t.setTextColor(Color.WHITE);
        t.setTextSize(20);
        t.setTypeface(null, Typeface.BOLD);
        GradientDrawable g = new GradientDrawable();
        g.setColor(Color.parseColor(isJava ? "#009A4E" : "#0A59F7"));
        g.setCornerRadius(12 * dp);
        t.setBackground(g);
        return t;
    }

    private LinearLayout buildLibCard(final LibraryStore.Item it) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout c = libCard(dp);

        // 头部：图标 + （标题 / 类型·大小·时间）
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);
        head.addView(makePackIcon(it, dp));

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams boxLp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        boxLp.leftMargin = (int) (12 * dp);
        box.setLayoutParams(boxLp);

        TextView title = mkText(it.name, 14, "#00713A");
        title.setTypeface(null, Typeface.BOLD);
        boolean isJava = LibraryStore.KIND_IMPORT.equals(it.kind);
        String sub = (isJava ? "Java 资源包" : "基岩版 .mcpack")
                + " · " + humanSize(it.size) + " · " + fmtTime(it.createdAt);
        if (it.inputName != null && !it.inputName.isEmpty())
            sub += "\n来自：" + it.inputName;
        TextView meta = mkText(sub, 12, "#5A6472");
        meta.setPadding(0, (int) (4 * dp), 0, 0);
        box.addView(title);
        box.addView(meta);
        head.addView(box);
        c.addView(head);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.addView(smallBtn("查看", "#6B7686", new View.OnClickListener() {
            @Override public void onClick(View v) { libShowDetail(it); }
        }));
        row.addView(smallBtn("下载", "#009A4E", new View.OnClickListener() {
            @Override public void onClick(View v) { libDoExport(it); }
        }));
        row.addView(smallBtn("删除", "#D94838", new View.OnClickListener() {
            @Override public void onClick(View v) { libConfirmDelete(it); }
        }));
        LinearLayout.LayoutParams rowLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rowLp.topMargin = (int) (8 * dp);
        c.addView(row, rowLp);
        return c;
    }

    private void libShowDetail(final LibraryStore.Item it) {
        File f = libraryStore.fileOf(it);
        String path = f != null ? f.getAbsolutePath() : "（文件已丢失）";
        new MaterialAlertDialogBuilder(this)
                .setTitle(it.name)
                .setMessage("类型：" + ("import".equals(it.kind) ? "导入的 Java 资源包" : "导出的基岩版 .mcpack")
                        + "\n大小：" + humanSize(it.size)
                        + "\n时间：" + fmtTime(it.createdAt)
                        + ("import".equals(it.kind) ? "" : "\n来源：" + (it.inputName.isEmpty() ? "—" : it.inputName))
                        + ("\n批次：" + ((it.batchId == null || it.batchId.isEmpty()) ? "—（旧记录）" : it.batchId))
                        + "\n状态：" + it.status
                        + "\n路径：" + path)
                .setPositiveButton("关闭", null)
                .setNeutralButton("下载", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        libDoExport(it);
                    }
                })
                .show();
    }

    /** 通过 SAF 让用户选择保存位置（离线、无需任何权限），实现「下载」。 */
    private void libDoExport(LibraryStore.Item it) {
        File f = libraryStore.fileOf(it);
        if (f == null) {
            Toast.makeText(this, "文件已丢失，无法下载", Toast.LENGTH_SHORT).show();
            return;
        }
        pendingExportPath = f.getAbsolutePath();
        pendingExportName = it.name;
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("application/octet-stream");
        i.putExtra(Intent.EXTRA_TITLE, it.name);
        try {
            startActivityForResult(i, REQ_LIB_EXPORT);
        } catch (Exception e) {
            Toast.makeText(this, "无法打开保存选择器", Toast.LENGTH_LONG).show();
        }
    }

    private void libConfirmDelete(final LibraryStore.Item it) {
        new MaterialAlertDialogBuilder(this)
                .setTitle("删除该记录？")
                .setMessage(it.name + "\n删除后将无法恢复。")
                .setPositiveButton("删除", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        iconCache.remove(it.stored);
                        libraryStore.delete(it.id);
                        refreshLibrary();
                        Toast.makeText(MainActivity.this, "已删除", Toast.LENGTH_SHORT).show();
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    // ============================================================ 底部导航
    private LinearLayout buildBottomNav(float dp) {
        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setBackgroundColor(Color.WHITE);
        nav.setPadding(0, (int) (6 * dp), 0, (int) (6 * dp));
        nav.setElevation(8 * dp);
        GradientDrawable top = new GradientDrawable();
        top.setColor(Color.parseColor("#E2E8E6"));
        top.setSize(ViewGroup.LayoutParams.MATCH_PARENT, (int) (1 * dp));
        String[] labels = {"转换", "设置", "资源库"};
        for (int i = 0; i < 3; i++) {
            final int idx = i;
            LinearLayout cell = new LinearLayout(this);
            cell.setOrientation(LinearLayout.VERTICAL);
            cell.setGravity(Gravity.CENTER);
            cell.setPadding(0, (int) (4 * dp), 0, (int) (4 * dp));
            LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            cell.setLayoutParams(clp);
            TextView t = new TextView(this);
            t.setText(labels[i]);
            t.setTextSize(13);
            t.setGravity(Gravity.CENTER);
            t.setTextColor(Color.parseColor("#9AA4B0"));
            cell.addView(t);
            cell.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { selectTab(idx); }
            });
            nav.addView(cell);
            navLabels[i] = t;
        }
        applyTabStyle();
        return nav;
    }

    private void selectTab(int idx) {
        currentTab = idx;
        convertScroll.setVisibility(idx == TAB_CONVERT ? View.VISIBLE : View.GONE);
        settingsScroll.setVisibility(idx == TAB_SETTINGS ? View.VISIBLE : View.GONE);
        libraryScroll.setVisibility(idx == TAB_LIBRARY ? View.VISIBLE : View.GONE);
        if (idx == TAB_CONVERT) bar.setTitle("MC 材质转换器 · 离线");
        else if (idx == TAB_SETTINGS) bar.setTitle("设置");
        else { bar.setTitle("资源库"); refreshLibrary(); }
        applyTabStyle();
    }

    private void applyTabStyle() {
        for (int i = 0; i < 3; i++) {
            if (navLabels[i] == null) continue;
            boolean on = i == currentTab;
            navLabels[i].setTextColor(Color.parseColor(on ? "#009A4E" : "#9AA4B0"));
            navLabels[i].setTypeface(null, on ? Typeface.BOLD : Typeface.NORMAL);
        }
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

    private TextView emptyHint(String s) {
        float dp = getResources().getDisplayMetrics().density;
        TextView t = mkText(s, 12, "#9AA4B0");
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (6 * dp);
        t.setLayoutParams(lp);
        return t;
    }

    /** 把控件以「全宽 + 统一上间距」加入转换页主体。 */
    private void addBody(View v) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * dp);
        body.addView(v, lp);
    }

    /** 把控件以「全宽 + 指定上间距」加入任意纵向容器。 */
    private void stack(LinearLayout parent, View v, int topMarginDp) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (topMarginDp * dp);
        parent.addView(v, lp);
    }

    private SwitchMaterial mkSwitch(String text) {
        float dp = getResources().getDisplayMetrics().density;
        SwitchMaterial sw = new SwitchMaterial(this);
        sw.setText(text);
        sw.setTextSize(13);
        sw.setChecked(false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
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

    private LinearLayout card(int bg, float dp) {
        // MaterialCardView 继承自 FrameLayout，直接向其中 addView 多个子控件会互相
        // 叠在左上角覆盖。这里改用纵向 LinearLayout + 圆角背景模拟卡片，子控件自然竖向排列。
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (14 * dp));
        GradientDrawable d = new GradientDrawable();
        d.setColor(bg);
        d.setCornerRadius(22 * dp);
        d.setStroke((int) (1 * dp), Color.parseColor("#E2E8E6"));
        c.setBackground(d);
        c.setElevation(4 * dp);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * dp);
        c.setLayoutParams(lp);
        return c;
    }

    private LinearLayout libCard(float dp) {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding((int) (14 * dp), (int) (12 * dp), (int) (14 * dp), (int) (12 * dp));
        GradientDrawable d = new GradientDrawable();
        d.setColor(0xFFF0F6F2);
        d.setCornerRadius(18 * dp);
        d.setStroke((int) (1 * dp), Color.parseColor("#E2E8E6"));
        c.setBackground(d);
        c.setElevation(3 * dp);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * dp);
        c.setLayoutParams(lp);
        return c;
    }

    private MaterialButton smallBtn(String text, String bg, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        MaterialButton b = new MaterialButton(this);
        b.setText(text);
        b.setTextColor(Color.WHITE);
        b.setTextSize(13);
        b.setBackgroundTintList(ColorStateList.valueOf(Color.parseColor(bg)));
        b.setCornerRadius((int) (100 * dp));
        b.setPadding((int) (16 * dp), (int) (8 * dp), (int) (16 * dp), (int) (8 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = (int) (8 * dp);
        b.setLayoutParams(lp);
        b.setOnClickListener(l);
        return b;
    }

    /** 分段选择器回调。 */
    private interface TogglePick { void onPick(int idx, String label); }

    private static final int PILL_ON = 0xFF009A4E;
    private static final int PILL_OFF = 0xFFE8EEF0;
    private static final int PILL_TX_ON = 0xFFFFFFFF;
    private static final int PILL_TX_OFF = 0xFF33414B;

    /** 生成一个可点击的胶囊 TextView（圆角背景 + padding），不依赖任何 MDC 状态机。 */
    private TextView makePill(String text, float dp) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(13);
        t.setGravity(Gravity.CENTER);
        t.setPadding((int) (14 * dp), (int) (8 * dp), (int) (14 * dp), (int) (8 * dp));
        t.setClickable(true);
        t.setFocusable(true);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = (int) (6 * dp);
        t.setLayoutParams(lp);
        return t;
    }

    /** 刷新胶囊选中态（背景圆角 + 文字色 + 加粗）。 */
    private void paintPill(TextView t, boolean on, float dp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(on ? PILL_ON : PILL_OFF);
        g.setCornerRadius(100 * dp);
        t.setBackground(g);
        t.setTextColor(on ? PILL_TX_ON : PILL_TX_OFF);
        t.setTypeface(null, on ? Typeface.BOLD : Typeface.NORMAL);
    }

    /**
     * 分段选择器：普通胶囊 TextView + setOnClickListener + 手动互斥高亮。
     * 替换掉原来的 MaterialButtonToggleGroup —— 后者依赖 check() 状态机且
     * MaterialButton(context,null,@style) 误用 defStyleAttr，动态创建时经常点不动。
     */
    private LinearLayout mkToggle(String[] labels, int defIdx, final TogglePick pick) {
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.HORIZONTAL);
        final TextView[] pills = new TextView[labels.length];
        for (int i = 0; i < labels.length; i++) {
            final int idx = i;
            final TextView t = makePill(labels[i], dp);
            t.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    for (int k = 0; k < pills.length; k++) paintPill(pills[k], k == idx, dp);
                    if (pick != null) pick.onPick(idx, labels[idx]);
                }
            });
            pills[i] = t;
            wrap.addView(t);
        }
        for (int k = 0; k < pills.length; k++) paintPill(pills[k], k == defIdx, dp);
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
        float dp = getResources().getDisplayMetrics().density;
        try {
            JSONArray arr = new JSONArray(json);
            final java.util.List<TextView> pills = new ArrayList<>();
            final java.util.List<String> ids = new ArrayList<>();
            for (int i = 0; i < arr.length(); i++) {
                JSONObject p = arr.getJSONObject(i);
                final String id = p.optString("id", "vv_standard");
                final String name = p.optString("name", id);
                final TextView chip = makePill(name, dp);
                chip.setTag(id);
                chip.setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View v) {
                        selectedPreset = id;
                        for (int k = 0; k < pills.size(); k++) {
                            TextView c = pills.get(k);
                            paintPill(c, c == chip, dp);
                        }
                        appendLog("预设：" + name);
                    }
                });
                pills.add(chip);
                ids.add(id);
                presetGroup.addView(chip);
            }
            // 初始高亮：按 selectedPreset 匹配，匹配不到则高亮第一个
            int hi = 0;
            for (int k = 0; k < ids.size(); k++) if (ids.get(k).equals(selectedPreset)) { hi = k; break; }
            for (int k = 0; k < pills.size(); k++) paintPill(pills.get(k), k == hi, dp);
            if (!ids.isEmpty() && hi < ids.size()) selectedPreset = ids.get(hi);
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
        if (req == REQ_SAVE) {
            if (res == RESULT_OK && data != null && data.getData() != null
                    && pendingOutPath != null) {
                try (InputStream in = new java.io.FileInputStream(pendingOutPath);
                     OutputStream out = getContentResolver().openOutputStream(data.getData())) {
                    copy(in, out);
                    ui(() -> Toast.makeText(this, "已保存到你选择的位置", Toast.LENGTH_LONG).show());
                } catch (Exception e) {
                    ui(() -> Toast.makeText(this, "另存为失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show());
                }
            }
            return;
        }
        if (req == REQ_LIB_EXPORT) {
            if (res == RESULT_OK && data != null && data.getData() != null
                    && pendingExportPath != null) {
                try (InputStream in = new java.io.FileInputStream(pendingExportPath);
                     OutputStream out = getContentResolver().openOutputStream(data.getData())) {
                    copy(in, out);
                    ui(() -> Toast.makeText(this, "已下载：" + pendingExportName, Toast.LENGTH_LONG).show());
                } catch (Exception e) {
                    ui(() -> Toast.makeText(this, "下载失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show());
                }
            }
            return;
        }
        if (req != REQ_FILE) return;
        if (res != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        lastInputName = queryDisplayName(uri);
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
            pendingOutPath = out.getAbsolutePath();
            // 落库存档：同一次转换的输入包 + 产物共享 batchId，供「按批次」归类
            String batchId = "B" + System.currentTimeMillis();
            libraryStore.addImport(cacheZip, lastInputName, batchId);
            LibraryStore.Item ex = libraryStore.addExport(out, out.getName(), lastInputName, batchId);
            File exFile = libraryStore.fileOf(ex);
            resultCard.setVisibility(View.VISIBLE);
            resultTitle.setText("✅ 转换完成 · 校验通过");
            resultMeta.setText(out.getName() + "（" + out.length() / 1024 + " KB）· 已用 "
                    + s.optString("elapsed_text", ""));
            buildPreview(s.optJSONObject("previews"));
            if (exFile != null) {
                try {
                    Uri shareUri = androidx.core.content.FileProvider.getUriForFile(
                            this, getPackageName() + ".fileprovider", exFile);
                    pendingImportUri = shareUri.toString();
                    importBtn.setVisibility(View.VISIBLE);
                    notifyDone(out.getName(), shareUri);
                } catch (Exception ignored) {
                    importBtn.setVisibility(View.GONE);
                }
            }
            saveAsBtn.setVisibility(View.VISIBLE);
            statusText.setText("已存入资源库：导入包与 .mcpack 均可在「资源库」中查看 / 下载。");
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

    /** 从内容 URI 取原始文件名（用于资源库存档展示）。 */
    private String queryDisplayName(Uri uri) {
        try {
            android.database.Cursor c = getContentResolver().query(uri,
                    new String[]{android.provider.OpenableColumns.DISPLAY_NAME}, null, null, null);
            if (c != null) {
                try {
                    if (c.moveToFirst()) {
                        String n = c.getString(0);
                        if (n != null && !n.isEmpty()) return n;
                    }
                } finally { c.close(); }
            }
        } catch (Exception ignored) { }
        return "input_pack.zip";
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

    /** 重新写入系统「下载」目录并反馈结果（不依赖任何权限，走 MediaStore）。 */
    private void doSaveToDownloads() {
        if (pendingOutPath == null) return;
        File out = new File(pendingOutPath);
        Uri exported = exportPack(out);
        if (exported != null) {
            pendingImportUri = exported.toString();
            importBtn.setVisibility(View.VISIBLE);
            notifyDone(out.getName(), exported);
            Toast.makeText(this, "已保存到「下载」目录：" + out.getName(), Toast.LENGTH_LONG).show();
        } else {
            Toast.makeText(this, "自动写入下载目录失败，请用「另存为…」选择位置",
                    Toast.LENGTH_LONG).show();
        }
    }

    /** 通过系统文件选择器（SAF）让用户自选保存位置，离线、无需任何权限。 */
    private void doSaveAs() {
        if (pendingOutPath == null) return;
        File out = new File(pendingOutPath);
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("application/octet-stream");
        i.putExtra(Intent.EXTRA_TITLE, out.getName());
        try {
            startActivityForResult(i, REQ_SAVE);
        } catch (Exception e) {
            Toast.makeText(this, "无法打开保存选择器", Toast.LENGTH_LONG).show();
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

    private static String humanSize(long b) {
        if (b < 1024) return b + " B";
        if (b < 1024 * 1024) return String.format(Locale.US, "%.1f KB", b / 1024f);
        return String.format(Locale.US, "%.2f MB", b / 1024f / 1024f);
    }

    private static String fmtTime(long ms) {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA).format(new Date(ms));
    }

    @Override
    protected void onDestroy() {
        pyExec.shutdownNow();
        super.onDestroy();
    }
}
