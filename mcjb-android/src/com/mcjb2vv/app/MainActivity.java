package com.mcjb2vv.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * MC 材质转换器 —— Android WebView 客户端壳。
 *
 * 转换核心（Python + Pillow + numpy）跑在服务端，本 APK 只负责：
 *   1) 承载已有 Web 前端（JS / fetch / FormData 分片上传全部原样复用）；
 *   2) 补齐 WebView 缺失的原生能力：文件选择器、.mcpack 下载、导入引导；
 *   3) 服务器地址可配置（公网网关 / 本机局域网直连）。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "mcjb_prefs";
    private static final String KEY_URL = "server_url";
    private static final int REQ_FILE = 1001;
    private static final int REQ_NOTIF = 1002;

    private WebView web;
    private ProgressBar progress;
    private View errOverlay;
    private TextView errText;
    private ValueCallback<Uri[]> filePathCallback;
    private String defaultUrl;

    // 下载完成提示兜底：部分 ROM 不派发给静态 receiver，Activity 存活期间动态注册一份
    private final BroadcastReceiver downloadReceiver = new DownloadReceiver();
    private boolean downloadReceiverRegistered = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        defaultUrl = getString(R.string.default_url);

        float dp = getResources().getDisplayMetrics().density;
        int pad = (int) (12 * dp);

        // ---- 标题栏 ----
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(Color.parseColor("#1F2430"));
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(pad, (int) (10 * dp), pad, (int) (10 * dp));

        TextView title = new TextView(this);
        title.setText("MC 材质转换器");
        title.setTextColor(Color.WHITE);
        title.setTextSize(16);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        bar.addView(title, titleLp);

        bar.addView(makeBarButton("刷新", new View.OnClickListener() {
            @Override public void onClick(View v) {
                hideError();
                web.reload();
            }
        }));
        bar.addView(makeBarButton("设置", new View.OnClickListener() {
            @Override public void onClick(View v) { showSettings(); }
        }));

        // ---- 进度条 ----
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgress(0);

        // ---- WebView + 错误覆盖层 ----
        FrameLayout frame = new FrameLayout(this);
        web = new WebView(this);
        frame.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        configureWebView();

        LinearLayout err = new LinearLayout(this);
        err.setOrientation(LinearLayout.VERTICAL);
        err.setBackgroundColor(Color.parseColor("#F7F8FA"));
        err.setGravity(Gravity.CENTER);
        err.setPadding((int) (24 * dp), (int) (24 * dp), (int) (24 * dp), (int) (24 * dp));
        errText = new TextView(this);
        errText.setTextSize(15);
        errText.setTextColor(Color.parseColor("#333A45"));
        errText.setGravity(Gravity.CENTER);
        err.addView(errText, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        err.addView(makeActionButton("重试连接", new View.OnClickListener() {
            @Override public void onClick(View v) {
                hideError();
                web.loadUrl(currentUrl());
            }
        }));
        err.addView(makeActionButton("修改服务地址", new View.OnClickListener() {
            @Override public void onClick(View v) { showSettings(); }
        }));
        errOverlay = err;
        errOverlay.setVisibility(View.GONE);
        frame.addView(errOverlay, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // ---- 组装 ----
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(progress, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (3 * dp)));
        root.addView(frame, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }

        web.loadUrl(currentUrl());
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!downloadReceiverRegistered) {
            IntentFilter f = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
            if (Build.VERSION.SDK_INT >= 33) {
                // Android 13+ / targetSdk 34：动态注册必须显式声明 exported 标志
                registerReceiver(downloadReceiver, f, Context.RECEIVER_EXPORTED);
            } else {
                registerReceiver(downloadReceiver, f);
            }
            downloadReceiverRegistered = true;
        }
    }

    private TextView makeBarButton(String text, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(Color.parseColor("#C9D3E0"));
        tv.setTextSize(14);
        tv.setPadding((int) (10 * dp), (int) (6 * dp), (int) (10 * dp), (int) (6 * dp));
        tv.setOnClickListener(l);
        tv.setClickable(true);
        tv.setFocusable(true);
        return tv;
    }

    private TextView makeActionButton(String text, View.OnClickListener l) {
        float dp = getResources().getDisplayMetrics().density;
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(Color.WHITE);
        tv.setTextSize(15);
        tv.setGravity(Gravity.CENTER);
        tv.setBackgroundColor(Color.parseColor("#3B6FD4"));
        tv.setPadding((int) (18 * dp), (int) (11 * dp), (int) (18 * dp), (int) (11 * dp));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (14 * dp);
        tv.setLayoutParams(lp);
        tv.setOnClickListener(l);
        tv.setClickable(true);
        tv.setFocusable(true);
        return tv;
    }

    private void configureWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setUserAgentString(s.getUserAgentString() + " MCJB2VV-Android/1.0");
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String scheme = u.getScheme();
                if (scheme != null && !scheme.equalsIgnoreCase("http")
                        && !scheme.equalsIgnoreCase("https")) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, u));
                    } catch (Exception ignored) { }
                    return true;
                }
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                progress.setVisibility(View.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progress.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) showError(networkHint());
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request,
                                            android.webkit.WebResourceResponse errorResponse) {
                if (request.isForMainFrame()) {
                    int code = errorResponse.getStatusCode();
                    showError("服务返回 HTTP " + code + "。\n" + networkHint());
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView webView,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) filePathCallback.onReceiveValue(null);
                filePathCallback = callback;
                try {
                    Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    i.setType("*/*");
                    i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                            "application/zip", "application/octet-stream", "*/*"});
                    if (params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                        i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    }
                    startActivityForResult(Intent.createChooser(i, "选择 Java 资源包"), REQ_FILE);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    Toast.makeText(MainActivity.this, "无法打开文件选择器", Toast.LENGTH_LONG).show();
                    return false;
                }
            }

            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
            }
        });

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent,
                                        String contentDisposition, String mimeType,
                                        long contentLength) {
                enqueueDownload(url, userAgent, contentDisposition, mimeType);
            }
        });
    }

    private String networkHint() {
        return "无法连接转换服务。\n\n"
                + "· 公网模式：需确认服务端正在运行，且地址栏保留了 ?sharecode= 参数；\n"
                + "· 局域网模式：点「修改服务地址」填 http://电脑IP:3000/，"
                + "手机与电脑需在同一 WiFi，且服务监听 0.0.0.0。\n\n"
                + "当前地址：" + currentUrl();
    }

    private void showError(String msg) {
        errText.setText(msg);
        errOverlay.setVisibility(View.VISIBLE);
    }

    private void hideError() {
        errOverlay.setVisibility(View.GONE);
    }

    private void enqueueDownload(String url, String ua, String cd, String mime) {
        String name = URLUtil.guessFileName(url, cd, "application/octet-stream");
        if (name == null || !name.contains(".")) name = "resourcepack.mcpack";
        try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null && !cookie.isEmpty()) req.addRequestHeader("Cookie", cookie);
            if (ua != null) req.addRequestHeader("User-Agent", ua);
            String ref = web.getUrl();
            req.addRequestHeader("Referer", ref != null ? ref : url);
            req.setMimeType("application/octet-stream");
            req.setTitle(name);
            req.setDescription("MC 材质包转换产物 · 完成后点击可导入基岩版");
            req.setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            long id = dm.enqueue(req);
            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                    .putLong("last_download_id", id)
                    .putString("last_download_name", name).apply();
            Toast.makeText(this, "开始下载：" + name, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } catch (Exception e2) {
                Toast.makeText(this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        }
    }

    private void showSettings() {
        float dp = getResources().getDisplayMetrics().density;
        final EditText et = new EditText(this);
        et.setSingleLine(true);
        et.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        et.setText(currentUrl());
        et.setSelection(et.getText().length());
        FrameLayout box = new FrameLayout(this);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        int pad = (int) (20 * dp);
        lp.leftMargin = pad; lp.rightMargin = pad; lp.topMargin = (int) (8 * dp);
        box.addView(et, lp);

        AlertDialog.Builder b = new AlertDialog.Builder(this);
        b.setTitle("转换服务地址");
        b.setMessage("默认连接已部署的公网网关。若在本机/局域网自建服务，可填 "
                + "http://电脑IP:3000/（服务需监听 0.0.0.0，手机与电脑同一 WiFi）。");
        b.setView(box);
        b.setPositiveButton("保存并打开", new android.content.DialogInterface.OnClickListener() {
            @Override public void onClick(android.content.DialogInterface d, int w) {
                String u = et.getText().toString().trim();
                if (u.isEmpty()) {
                    Toast.makeText(MainActivity.this, "地址不能为空", Toast.LENGTH_SHORT).show();
                    return;
                }
                if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://" + u;
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, u).apply();
                hideError();
                web.loadUrl(u);
            }
        });
        b.setNeutralButton("恢复默认", new android.content.DialogInterface.OnClickListener() {
            @Override public void onClick(android.content.DialogInterface d, int w) {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(KEY_URL).apply();
                hideError();
                web.loadUrl(defaultUrl);
            }
        });
        b.setNegativeButton("取消", null);
        b.show();
    }

    private String currentUrl() {
        return getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, defaultUrl);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_FILE) return;
        if (filePathCallback == null) return;
        Uri[] results = null;
        if (resultCode == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                int n = data.getClipData().getItemCount();
                results = new Uri[n];
                for (int i = 0; i < n; i++) {
                    results[i] = data.getClipData().getItemAt(i).getUri();
                }
            } else if (data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
        }
        filePathCallback.onReceiveValue(results);   // 取消时必须回传 null，否则下次点不动
        filePathCallback = null;
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (downloadReceiverRegistered) {
            try { unregisterReceiver(downloadReceiver); } catch (Exception ignored) { }
            downloadReceiverRegistered = false;
        }
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
