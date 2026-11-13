package com.mcjb2vv.app;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.util.concurrent.atomic.AtomicBoolean;

/**
 * MC-JB2VV Android 外壳：
 * 1) 启动 Chaquopy 内嵌 Python 运行时；
 * 2) 在后台线程启动 Flask 本地服务（由 python 模块 mcjb_app 提供）；
 * 3) WebView 加载本地 Flask 页面，前端通过 127.0.0.1:5000 与转换内核通信。
 */
public class MainActivity extends Activity {

    private WebView webView;
    private static final AtomicBoolean serverStarted = new AtomicBoolean(false);
    private static final int SERVER_PORT = 5000;
    private static final int LOAD_DELAY_MS = 1800;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.getSettings().setAllowFileAccess(true);
        webView.getSettings().setAllowContentAccess(true);
        webView.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webView.getSettings().setLoadWithOverviewMode(true);
        webView.getSettings().setUseWideViewPort(true);

        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        startPythonServer();

        // 等待 Flask 起来后再加载页面
        webView.postDelayed(this::loadFrontend, LOAD_DELAY_MS);
    }

    private void startPythonServer() {
        if (serverStarted.compareAndSet(false, true)) {
            new Thread(() -> {
                try {
                    Python.getInstance().getModule("mcjb_app").callAttr("run");
                } catch (Exception e) {
                    e.printStackTrace();
                }
            }).start();
        }
    }

    private void loadFrontend() {
        String url = Python.getInstance().getModule("mcjb_app").callAttr("url").toString();
        webView.loadUrl(url);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
