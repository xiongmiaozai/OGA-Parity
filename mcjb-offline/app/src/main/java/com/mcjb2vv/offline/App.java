package com.mcjb2vv.offline;

import android.app.Application;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/** 启动 Chaquopy Python 运行时；完全离线，无任何网络初始化。 */
public class App extends Application {

    private volatile boolean pythonReady = false;
    private volatile String initError = null;
    private final Object lock = new Object();

    public boolean isPythonReady() { return pythonReady; }
    public String getInitError() { return initError; }

    @Override
    public void onCreate() {
        super.onCreate();
        // 首次启动会解压内置 CPython stdlib，放到后台避免 ANR
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    if (!Python.isStarted()) {
                        Python.start(new AndroidPlatform(getApplicationContext()));
                    }
                    pythonReady = true;
                } catch (Throwable t) {
                    initError = t.toString();
                } finally {
                    synchronized (lock) { lock.notifyAll(); }
                }
            }
        }, "chaquopy-init").start();
    }

    /** 转换线程等待 Python 运行时就绪，最多 60 秒。 */
    public boolean awaitPython(long timeoutMs) {
        if (pythonReady || initError != null) return pythonReady;
        synchronized (lock) {
            try { lock.wait(timeoutMs); } catch (InterruptedException ignored) { }
        }
        return pythonReady;
    }
}
