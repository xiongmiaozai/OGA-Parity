package com.mcjb2vv.offline;

import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.card.MaterialCardView;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 资源库面板（独立于主界面）。
 * 列出「导入的 Java 资源包」与「导出的基岩版 .mcpack」两类存档，
 * 每条支持：查看（详情）、下载（SAF 导出到本机任意位置，含下载目录）、删除。
 * 全部离线、无需任何存储权限。
 */
public class LibraryActivity extends AppCompatActivity {

    private static final int REQ_EXPORT = 3001;
    private LibraryStore store;
    private LinearLayout importList, exportList;
    private String pendingExportPath = null;
    private String pendingExportName = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        float dp = getResources().getDisplayMetrics().density;
        store = new LibraryStore(this);

        MaterialToolbar bar = new MaterialToolbar(this);
        bar.setTitle("资源库");
        bar.setTitleTextColor(Color.WHITE);
        bar.setBackgroundColor(Color.parseColor("#00713A"));
        bar.setNavigationIcon(com.google.android.material.R.drawable.abc_ic_ab_back_material);
        bar.setNavigationOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });

        ScrollView rootScroll = new ScrollView(this);
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding((int) (16 * dp), (int) (14 * dp), (int) (16 * dp), (int) (16 * dp));

        body.addView(sectionLabel("导入的 Java 资源包"));
        importList = new LinearLayout(this);
        importList.setOrientation(LinearLayout.VERTICAL);
        body.addView(importList);

        body.addView(sectionLabel("导出的基岩版 .mcpack"));
        exportList = new LinearLayout(this);
        exportList.setOrientation(LinearLayout.VERTICAL);
        body.addView(exportList);

        rootScroll.addView(body);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(rootScroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        refresh();
    }

    private void refresh() {
        importList.removeAllViews();
        exportList.removeAllViews();
        List<LibraryStore.Item> items = store.all();
        boolean anyImport = false, anyExport = false;
        for (LibraryStore.Item it : items) {
            if (LibraryStore.KIND_IMPORT.equals(it.kind)) {
                importList.addView(buildCard(it));
                anyImport = true;
            } else {
                exportList.addView(buildCard(it));
                anyExport = true;
            }
        }
        if (!anyImport) importList.addView(emptyHint("还没有导入记录，去主界面转换后会自动存档"));
        if (!anyExport) exportList.addView(emptyHint("还没有导出记录，转换成功后会自动出现在这里"));
    }

    private View buildCard(final LibraryStore.Item it) {
        float dp = getResources().getDisplayMetrics().density;
        MaterialCardView c = new MaterialCardView(this);
        c.setCardBackgroundColor(android.content.res.ColorStateList.valueOf(0xFFF0F6F2));
        c.setRadius(18 * dp);
        c.setCardElevation(3 * dp);
        c.setContentPadding((int) (14 * dp), (int) (12 * dp), (int) (14 * dp), (int) (12 * dp));
        c.setStrokeColor(android.content.res.ColorStateList.valueOf(Color.parseColor("#E2E8E6")));
        c.setStrokeWidth((int) (1 * dp));
        LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        clp.topMargin = (int) (8 * dp);
        c.setLayoutParams(clp);

        // MaterialCardView 是 FrameLayout：直接 addView 多子控件会叠在左上角互相覆盖。
        // 用一个纵向 LinearLayout 承载内容，卡片自然竖向排列。
        LinearLayout inner = new LinearLayout(this);
        inner.setOrientation(LinearLayout.VERTICAL);

        TextView title = mkText(it.name, 14, "#00713A");
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        String sub = humanSize(it.size) + " · " + fmtTime(it.createdAt);
        if (it.inputName != null && !it.inputName.isEmpty())
            sub += "\n来自：" + it.inputName;
        TextView meta = mkText(sub, 12, "#5A6472");
        meta.setPadding(0, (int) (4 * dp), 0, (int) (8 * dp));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        MaterialButton viewBtn = smallBtn("查看", "#6B7686", new View.OnClickListener() {
            @Override public void onClick(View v) { showDetail(it); }
        });
        MaterialButton dlBtn = smallBtn("下载", "#009A4E", new View.OnClickListener() {
            @Override public void onClick(View v) { doExport(it); }
        });
        MaterialButton delBtn = smallBtn("删除", "#D94838", new View.OnClickListener() {
            @Override public void onClick(View v) { confirmDelete(it); }
        });
        row.addView(viewBtn);
        row.addView(dlBtn);
        row.addView(delBtn);

        inner.addView(title);
        inner.addView(meta);
        inner.addView(row);
        c.addView(inner);
        return c;
    }

    private void showDetail(LibraryStore.Item it) {
        File f = store.fileOf(it);
        String path = f != null ? f.getAbsolutePath() : "（文件已丢失）";
        new MaterialAlertDialogBuilder(this)
                .setTitle(it.name)
                .setMessage("类型：" + ("import".equals(it.kind) ? "导入的 Java 资源包" : "导出的基岩版 .mcpack")
                        + "\n大小：" + humanSize(it.size)
                        + "\n时间：" + fmtTime(it.createdAt)
                        + ("import".equals(it.kind) ? "" : "\n来源：" + (it.inputName.isEmpty() ? "—" : it.inputName))
                        + "\n状态：" + it.status
                        + "\n路径：" + path)
                .setPositiveButton("关闭", null)
                .setNeutralButton("下载", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        doExport(it);
                    }
                })
                .show();
    }

    /** 通过 SAF 让用户选择保存位置（离线、无需任何权限），实现「下载」。 */
    private void doExport(LibraryStore.Item it) {
        File f = store.fileOf(it);
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
            startActivityForResult(i, REQ_EXPORT);
        } catch (Exception e) {
            Toast.makeText(this, "无法打开保存选择器", Toast.LENGTH_LONG).show();
        }
    }

    private void confirmDelete(final LibraryStore.Item it) {
        new MaterialAlertDialogBuilder(this)
                .setTitle("删除该记录？")
                .setMessage(it.name + "\n删除后将无法恢复。")
                .setPositiveButton("删除", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        store.delete(it.id);
                        refresh();
                        Toast.makeText(LibraryActivity.this, "已删除", Toast.LENGTH_SHORT).show();
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == REQ_EXPORT && res == RESULT_OK && data != null && data.getData() != null
                && pendingExportPath != null) {
            try (InputStream in = new FileInputStream(pendingExportPath);
                 OutputStream out = getContentResolver().openOutputStream(data.getData())) {
                byte[] buf = new byte[1 << 16];
                int r;
                while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
                Toast.makeText(this, "已下载：" + pendingExportName, Toast.LENGTH_LONG).show();
            } catch (Exception e) {
                Toast.makeText(this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        }
    }

    // ----------------------------------------------------------- 视图工具
    private TextView mkText(String s, int size, String color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(Color.parseColor(color));
        return t;
    }

    private TextView sectionLabel(String s) {
        TextView t = mkText(s, 13, "#009A4E");
        t.setTypeface(null, android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, (int) (14 * getResources().getDisplayMetrics().density), 0,
                (int) (4 * getResources().getDisplayMetrics().density));
        t.setLayoutParams(lp);
        return t;
    }

    private TextView emptyHint(String s) {
        TextView t = mkText(s, 12, "#9AA4B0");
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (6 * getResources().getDisplayMetrics().density);
        t.setLayoutParams(lp);
        return t;
    }

    private MaterialButton smallBtn(String text, String bg, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        MaterialButton b = new MaterialButton(this);
        b.setText(text);
        b.setTextColor(Color.WHITE);
        b.setTextSize(13);
        b.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.parseColor(bg)));
        b.setCornerRadius((int) (100 * dp));
        b.setPadding((int) (16 * dp), (int) (8 * dp), (int) (16 * dp), (int) (8 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = (int) (8 * dp);
        b.setLayoutParams(lp);
        b.setOnClickListener(l);
        return b;
    }

    private static String humanSize(long b) {
        if (b < 1024) return b + " B";
        if (b < 1024 * 1024) return String.format(Locale.US, "%.1f KB", b / 1024f);
        return String.format(Locale.US, "%.2f MB", b / 1024f / 1024f);
    }

    private static String fmtTime(long ms) {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA).format(new Date(ms));
    }
}
