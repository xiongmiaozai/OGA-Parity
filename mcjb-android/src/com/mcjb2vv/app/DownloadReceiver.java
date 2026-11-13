package com.mcjb2vv.app;

import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;

/** 下载完成后提示用户点击，用 Minecraft 基岩版（或其它认领 .mcpack 的应用）导入。 */
public class DownloadReceiver extends BroadcastReceiver {

    private static final String CHANNEL = "mcjb_download";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (!DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction())) return;
        long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
        if (id < 0) return;
        SharedPreferences p = ctx.getSharedPreferences("mcjb_prefs", Context.MODE_PRIVATE);
        if (id != p.getLong("last_download_id", -1L)) return;
        String name = p.getString("last_download_name", "resourcepack.mcpack");

        DownloadManager dm = (DownloadManager) ctx.getSystemService(Context.DOWNLOAD_SERVICE);
        Uri uri = dm.getUriForDownloadedFile(id);
        if (uri == null) return;
        // 只提示真正下载成功的
        DownloadManager.Query q = new DownloadManager.Query().setFilterById(id);
        Cursor c = dm.query(q);
        try {
            if (c == null || !c.moveToFirst()) return;
            int st = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
            if (st != DownloadManager.STATUS_SUCCESSFUL) return;
        } finally {
            if (c != null) c.close();
        }

        NotificationManager nm =
                (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "转换包下载完成", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("资源包转换完成后的导入提醒");
            nm.createNotificationChannel(ch);
        }

        Intent open = new Intent(Intent.ACTION_VIEW);
        open.setDataAndType(uri, "application/octet-stream");
        open.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi;
        try {
            pi = PendingIntent.getActivity(ctx, (int) id, open, flags);
        } catch (Exception e) {
            return;
        }

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(ctx, CHANNEL)
                : new Notification.Builder(ctx);
        b.setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("材质包转换完成")
                .setContentText(name + " · 点击导入 Minecraft 基岩版")
                .setContentIntent(pi)
                .setAutoCancel(true);
        nm.notify((int) (id & 0x7fffffff), b.build());
    }
}
