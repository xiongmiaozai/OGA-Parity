package com.mcjb2vv.offline;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * 本地资源库：把「导入的 Java 资源包」与「导出的基岩版 .mcpack」稳定存档到应用私有
 * 目录，并用 library.json 维护索引，供「资源库」面板查看与导出（SAF 下载）。
 *
 * 为什么不用 MediaStore.Downloads 自动写公共下载目录：
 *   Android 11+ 对非媒体文件（.mcpack/.zip 属 application/octet-stream）写入
 *   MediaStore.Downloads 需要 WRITE_EXTERNAL_STORAGE / MANAGE_EXTERNAL_STORAGE，
 *   与「完全离线、最小权限」冲突且多数设备必然失败。故产物落地到私有库目录，
 *   由用户在面板里用 SAF（ACTION_CREATE_DOCUMENT）主动导出到任意位置（含下载目录）。
 */
public class LibraryStore {

    public static final String KIND_IMPORT = "import";
    public static final String KIND_EXPORT = "export";

    private final File root, importsDir, exportsDir, index;

    public LibraryStore(Context ctx) {
        root = new File(ctx.getFilesDir(), "mcjb/library");
        importsDir = new File(root, "imports");
        exportsDir = new File(root, "exports");
        importsDir.mkdirs();
        exportsDir.mkdirs();
        index = new File(root, "library.json");
    }

    /** 单条记录。 */
    public static class Item {
        public String id, kind, name, stored, inputName, status, batchId;
        public long size, createdAt;

        Item(JSONObject o) {
            id = o.optString("id");
            kind = o.optString("kind");
            name = o.optString("name");
            stored = o.optString("stored");
            inputName = o.optString("inputName");
            status = o.optString("status", "ok");
            batchId = o.optString("batchId", "");
            size = o.optLong("size");
            createdAt = o.optLong("createdAt");
        }

        JSONObject toJson() throws org.json.JSONException {
            return new JSONObject()
                    .put("id", id)
                    .put("kind", kind)
                    .put("name", name)
                    .put("stored", stored)
                    .put("inputName", inputName)
                    .put("status", status)
                    .put("size", size)
                    .put("createdAt", createdAt)
                    .put("batchId", batchId);
        }
    }

    private JSONObject load() throws org.json.JSONException {
        try {
            if (!index.exists()) return new JSONObject().put("items", new JSONArray());
            JSONObject o = new JSONObject(readFile(index));
            if (!o.has("items")) o.put("items", new JSONArray());
            return o;
        } catch (Exception e) {
            return new JSONObject().put("items", new JSONArray());
        }
    }

    private void save(JSONObject o) throws Exception {
        try (FileOutputStream fos = new FileOutputStream(index)) {
            fos.write(o.toString(2).getBytes("UTF-8"));
        }
    }

    private static String readFile(File f) throws Exception {
        try (InputStream in = new FileInputStream(f)) {
            byte[] b = new byte[(int) f.length()];
            int n = 0;
            while (n < b.length) {
                int r = in.read(b, n, b.length - n);
                if (r < 0) break;
                n += r;
            }
            return new String(b, 0, n, "UTF-8");
        }
    }

    private static String sanitize(String name) {
        if (name == null || name.isEmpty()) name = "pack";
        // 仅保留安全字符，避免路径穿越 / 非法文件名
        return name.replaceAll("[^\\w.\\-\\u4e00-\\u9fa5]", "_");
    }

    /** 复制源文件进库，返回存档记录（失败时返回 null）。 */
    private Item store(File src, String kind, String origName, String inputName, String batchId) {
        if (src == null || !src.exists()) return null;
        try {
            long ts = System.currentTimeMillis();
            File dir = KIND_IMPORT.equals(kind) ? importsDir : exportsDir;
            File dst = new File(dir, ts + "_" + sanitize(origName));
            copy(new FileInputStream(src), new FileOutputStream(dst));
            Item it = new Item(new JSONObject());
            it.id = kind.charAt(0) + "_" + ts;
            it.kind = kind;
            it.name = origName;
            it.stored = dst.getName();
            it.inputName = inputName == null ? "" : inputName;
            it.batchId = batchId == null ? "" : batchId;
            it.status = "ok";
            it.size = dst.length();
            it.createdAt = ts;
            JSONObject o = load();
            o.getJSONArray("items").put(it.toJson());
            save(o);
            return it;
        } catch (Exception e) {
            return null;
        }
    }

    /** 一次转换（输入包 + 产物）共享同一 batchId，供「按批次」归类。 */
    public Item addImport(File src, String origName, String batchId) {
        return store(src, KIND_IMPORT, origName, "", batchId);
    }

    public Item addExport(File src, String origName, String inputName, String batchId) {
        return store(src, KIND_EXPORT, origName, inputName, batchId);
    }

    /**
     * 从资源包 zip 内读取自带图标，返回图片字节；找不到或异常返回 null。
     *
     * 关键点：Java 资源包图标通常是根目录 pack.png，基岩版是 pack_icon.png，
     * 但很多包会把内容整体包一层子目录（如 "Faithful/pack.png"），或大小写不同、
     * 用 .jpg。因此这里遍历全部条目，取「文件名」（忽略路径）做大小写不敏感匹配，
     * 根目录者优先、命名优先级高者优先，避免因路径差异而读不到图标。
     */
    public byte[] iconOf(Item it) {
        File f = fileOf(it);
        if (f == null) return null;
        final String[] cand = {
                "pack_icon.png", "pack_icon.jpg", "pack_icon.jpeg",
                "pack.png", "pack.jpg", "pack.jpeg"
        };
        java.util.zip.ZipFile zf = null;
        try {
            zf = new java.util.zip.ZipFile(f);
            java.util.zip.ZipEntry best = null;
            int bestScore = -1;
            java.util.Enumeration<? extends java.util.zip.ZipEntry> en = zf.entries();
            while (en.hasMoreElements()) {
                java.util.zip.ZipEntry e = en.nextElement();
                if (e.isDirectory()) continue;
                String full = e.getName();
                String base = full.substring(full.lastIndexOf('/') + 1).toLowerCase(java.util.Locale.US);
                boolean isImg = base.endsWith(".png") || base.endsWith(".jpg") || base.endsWith(".jpeg");
                int pri = -1;
                for (int i = 0; i < cand.length; i++) if (base.equals(cand[i])) { pri = cand.length - i; break; }
                // 兜底：包内没有标准命名时，取文件名含 icon/logo 的图片（如 logo.png / myicon.jpg）
                boolean fallbackName = pri < 0 && isImg
                        && (base.contains("icon") || base.contains("logo"));
                if (pri < 0 && !fallbackName) continue;
                boolean atRoot = full.indexOf('/') < 0;
                int score = (pri < 0 ? 1 : (pri + 1) * 10) + (atRoot ? 1 : 0);  // 根目录同类命名优先
                long sz = e.getSize();
                if (sz == 0 || sz > 4 * 1024 * 1024) continue;   // 跳过空/超大，防 OOM
                if (score > bestScore) { bestScore = score; best = e; }
            }
            if (best == null) return null;
            try (InputStream in = zf.getInputStream(best);
                 java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream()) {
                byte[] buf = new byte[8192];
                int r, total = 0;
                while ((r = in.read(buf)) > 0) {
                    total += r;
                    if (total > 4 * 1024 * 1024) return null;   // 双保险
                    bos.write(buf, 0, r);
                }
                return bos.toByteArray();
            }
        } catch (Exception ignored) {
        } finally {
            if (zf != null) try { zf.close(); } catch (Exception ignored) { }
        }
        return null;
    }

    /** 返回全部记录（新→旧）。 */
    public List<Item> all() {
        List<Item> list = new ArrayList<>();
        try {
            JSONArray arr = load().getJSONArray("items");
            for (int i = arr.length() - 1; i >= 0; i--) {
                list.add(new Item(arr.getJSONObject(i)));
            }
        } catch (Exception ignored) {
        }
        return list;
    }

    public Item byId(String id) {
        for (Item it : all()) if (it.id.equals(id)) return it;
        return null;
    }

    /** 实际文件（私有库内），不存在返回 null。 */
    public File fileOf(Item it) {
        if (it == null || it.stored == null || it.stored.isEmpty()) return null;
        File f = new File(KIND_IMPORT.equals(it.kind) ? importsDir : exportsDir, it.stored);
        return f.exists() ? f : null;
    }

    /** 删除一条记录及其文件。 */
    public void delete(String id) {
        try {
            JSONObject o = load();
            JSONArray arr = o.getJSONArray("items");
            JSONArray next = new JSONArray();
            for (int i = 0; i < arr.length(); i++) {
                JSONObject j = arr.getJSONObject(i);
                if (j.optString("id").equals(id)) {
                    File f = new File(KIND_IMPORT.equals(j.optString("kind")) ? importsDir : exportsDir,
                            j.optString("stored"));
                    if (f.exists()) f.delete();
                } else {
                    next.put(j);
                }
            }
            o.put("items", next);
            save(o);
        } catch (Exception ignored) {
        }
    }

    private static void copy(InputStream in, OutputStream out) throws Exception {
        byte[] buf = new byte[1 << 16];
        int r;
        while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
        in.close();
        out.close();
    }
}
