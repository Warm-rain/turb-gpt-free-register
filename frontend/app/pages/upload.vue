<script setup lang="ts">
definePageMeta({ layout: false });
useHead({
  title: "公共账号上传 · Registrator",
  meta: [{ name: "referrer", content: "no-referrer" }],
});
const api = useApi();
const key = ref("");
const text = ref("");
const fileName = ref("");
const busy = ref(false);
const error = ref("");
const result = ref<any>(null);
const lines = computed(() => text.value.split(/\r?\n/).filter((line) => line.trim()).length);

// 上传 Key 保存在本机浏览器：可以存多个（例如不同上传者共用一台电脑时各存一个），
// 刷新或重开浏览器后可直接选用；服务器只保存管理员分配的 Key 本身，不接收本机副本。
const STORAGE_KEY = "registrator_upload_keys_v1";
interface SavedKey {
  label: string;
  key: string;
  saved_at: string;
}
const savedKeys = ref<SavedKey[]>([]);
const saveLabel = ref("");
const storageNotice = ref("");

function defaultKeyLabel(value: string) {
  return value.length > 4 ? `Key …${value.slice(-4)}` : "已保存的 Key";
}
function maskKey(value: string) {
  return value.length > 8 ? `${value.slice(0, 4)}…${value.slice(-4)}` : "••••••";
}
function loadSavedKeys() {
  if (typeof localStorage === "undefined") return;
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    savedKeys.value = (Array.isArray(parsed) ? parsed : [])
      .filter((item: any) => item && typeof item.key === "string" && item.key.trim())
      .map((item: any) => ({
        label: String(item.label || "") || defaultKeyLabel(String(item.key)),
        key: String(item.key),
        saved_at: String(item.saved_at || ""),
      }));
  } catch {
    savedKeys.value = [];
  }
  if (!key.value.trim() && savedKeys.value.length) key.value = savedKeys.value[0]?.key || "";
}
function persistSavedKeys() {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(savedKeys.value));
  } catch {
    error.value = "无法写入浏览器本地存储，Key 未保存";
  }
}
function saveCurrentKey() {
  const value = key.value.trim();
  if (!value) {
    error.value = "请先输入上传 Key";
    return;
  }
  error.value = "";
  const label = saveLabel.value.trim() || defaultKeyLabel(value);
  const entry: SavedKey = { label, key: value, saved_at: new Date().toISOString() };
  savedKeys.value = [entry, ...savedKeys.value.filter((item) => item.key !== value)];
  saveLabel.value = "";
  persistSavedKeys();
  storageNotice.value = `已在本机保存「${label}」`;
}
function useSavedKey(item: SavedKey) {
  key.value = item.key;
  storageNotice.value = `已填入「${item.label}」`;
}
function removeSavedKey(item: SavedKey) {
  savedKeys.value = savedKeys.value.filter((entry) => entry.key !== item.key);
  if (key.value.trim() === item.key) key.value = "";
  persistSavedKeys();
  storageNotice.value = `已删除「${item.label}」`;
}
function clearSavedKeys() {
  if (!window.confirm("删除本机保存的全部上传 Key？")) return;
  savedKeys.value = [];
  key.value = "";
  persistSavedKeys();
  storageNotice.value = "已清空本机保存的 Key";
}
// 成功上传后把用过的 Key 排到最前，下次打开自动填入的是最近使用的那一个。
function rememberUsedKey() {
  const value = key.value.trim();
  const used = savedKeys.value.find((item) => item.key === value);
  if (!used) return;
  savedKeys.value = [used, ...savedKeys.value.filter((item) => item.key !== value)];
  persistSavedKeys();
}

async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  error.value = "";
  result.value = null;
  try {
    if (file.size > 10 * 1024 * 1024) throw new Error("单次导入文件不能超过 10 MiB");
    text.value = await file.text();
    fileName.value = file.name;
  } catch (cause: any) {
    error.value = cause?.message || "读取文件失败";
  } finally {
    input.value = "";
  }
}

async function submit() {
  if (busy.value) return;
  error.value = "";
  result.value = null;
  if (!key.value.trim()) { error.value = "请输入上传 Key"; return; }
  if (!text.value.trim()) { error.value = "请粘贴账号内容或选择文本文件"; return; }
  if (lines.value > 5000) { error.value = "单次最多导入 5000 行"; return; }
  if (new Blob([text.value]).size > 10 * 1024 * 1024) { error.value = "单次导入内容不能超过 10 MiB"; return; }
  busy.value = true;
  try {
    result.value = await api.request("/api/public/accounts/import", {
      method: "POST",
      headers: { "X-Upload-Key": key.value.trim() },
      body: { text: text.value },
    });
    if (result.value.ok === false) throw new Error(result.value.error || "上传失败");
    rememberUsedKey();
    text.value = "";
    fileName.value = "";
  } catch (cause: any) {
    error.value = cause?.message || "上传失败，请稍后重试";
    if (cause?.data) result.value = cause.data;
  } finally {
    busy.value = false;
  }
}
onMounted(loadSavedKeys);
</script>

<template>
  <div class="public-shell">
    <header class="public-header">
      <NuxtLink class="brand" to="/upload"><img src="/brand.svg" width="34" height="34" alt="" />Registrator</NuxtLink>
      <NuxtLink to="/login" class="muted">管理员登录 ↗</NuxtLink>
    </header>
    <main class="upload-main">
      <div class="upload-heading">
        <div class="eyebrow">PRIVATE KEY REQUIRED</div>
        <h1>上传账号到池内</h1>
        <p>使用有效 Key 将账号批量加入账号池。</p>
      </div>
      <section class="card upload-card">
        <form class="card-body stack" @submit.prevent="submit">
          <label class="field">上传 Key
            <input v-model="key" class="input" type="password" autocomplete="off" placeholder="输入后台分配的上传 Key" :disabled="busy" required />
            <small>Key 只用于上传请求，请勿分享给无关人员。</small>
          </label>
          <div class="saved-keys">
            <div class="saved-keys-actions">
              <input v-model="saveLabel" class="input" maxlength="40" placeholder="备注名（可选，例如 运营-张三）" :disabled="busy" aria-label="本机保存的备注名" />
              <button type="button" class="btn btn-sm" :disabled="busy || !key.trim()" @click="saveCurrentKey">保存到本机</button>
              <button v-if="savedKeys.length" type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="clearSavedKeys">清空</button>
            </div>
            <ul v-if="savedKeys.length" class="saved-key-list">
              <li v-for="item in savedKeys" :key="item.key">
                <span class="saved-key-label">{{ item.label }}</span>
                <code>{{ maskKey(item.key) }}</code>
                <button type="button" class="btn btn-sm" :disabled="busy" @click="useSavedKey(item)">使用</button>
                <button type="button" class="btn btn-sm btn-danger" :disabled="busy" @click="removeSavedKey(item)">删除</button>
              </li>
            </ul>
            <small class="muted">Key 只保存在本机浏览器（localStorage），不会上传到服务器；共用电脑请用完及时删除。</small>
            <p v-if="storageNotice" class="muted" role="status">{{ storageNotice }}</p>
          </div>
          <label class="field">从文本文件导入
            <input type="file" accept=".txt,.csv,text/plain,text/csv" :disabled="busy" @change="readFile" />
            <small v-if="fileName" class="muted">已读取 {{ fileName }}，可在下方修改。</small>
          </label>
          <label class="field">账号内容
            <textarea v-model="text" class="textarea mono upload-text" rows="12" :disabled="busy" spellcheck="false" autocomplete="off" placeholder="user@example.com--password--2FA_SECRET--access_token" />
          </label>
          <small class="muted">{{ lines }} 行。格式：邮箱--密码--2FA（可追加 --AT）；不带 AT 的新账号导入后自动查活。最多 5000 行 / 10 MiB。</small>
          <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
          <section v-if="result" class="import-result" aria-live="polite">
            <strong v-if="result.ok">上传完成：已解析 {{ result.parsed }} 个，新增 {{ result.inserted }} 个，跳过 {{ result.skipped }} 个。</strong>
            <ul v-if="result.errors?.length"><li v-for="(item, index) in result.errors" :key="index">第 {{ item.line }} 行：{{ item.reason }}</li></ul>
            <ul v-if="result.skipped_details?.length"><li v-for="item in result.skipped_details" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
            <p v-if="result.user_names_fetched != null" class="muted">已获取 {{ result.user_names_fetched }} 个用户名。</p>
            <p v-if="result.live_checks_queued != null" class="muted">已加入自动查活队列 {{ result.live_checks_queued }} 个。</p>
            <ul v-if="result.live_check_warnings?.length"><li v-for="item in result.live_check_warnings" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
            <ul v-if="result.user_name_warnings?.length"><li v-for="item in result.user_name_warnings" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
          </section>
          <button class="btn btn-primary" type="submit" :disabled="busy || !key.trim() || !text.trim()">{{ busy ? "正在上传，请稍候…" : "验证 Key 并上传" }}</button>
        </form>
      </section>
    </main>
    <footer class="public-footer">Registrator · 公共账号上传</footer>
  </div>
</template>

<style scoped>
.upload-main { width: 100%; max-width: 760px; margin: 35px auto 50px; padding: 0 24px; }
.upload-heading { text-align: center; margin: 15px 0 38px; }
.upload-heading h1 { font-size: 35px; letter-spacing: -1px; font-weight: 600; }
.upload-heading p { color: #91a299; font-size: 13px; margin-top: 12px; }
.upload-card { max-width: 760px; margin: 0 auto; }
.upload-text { width: 100%; min-height: 260px; }
.saved-keys { display: flex; flex-direction: column; gap: 8px; }
.saved-keys-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.saved-keys-actions .input { flex: 1; min-width: 180px; }
.saved-key-list { list-style: none; margin: 2px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.saved-key-list li {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; border: 1px solid #e6ebf1; border-radius: 6px; background: #fafbfc;
}
.saved-key-label { font-size: 12px; font-weight: 500; min-width: 90px; overflow-wrap: anywhere; }
.saved-key-list code { flex: 1; min-width: 90px; font-size: 11px; color: #7a8797; overflow-wrap: anywhere; }
.import-result { padding: 13px 15px; border: 1px solid #bfe5d6; border-radius: 7px; background: #f1fbf7; overflow-wrap: anywhere; }
.import-result ul { margin: 8px 0 0; padding-left: 20px; }
@media (max-width: 680px) { .upload-heading h1 { font-size: 28px; } .upload-main { padding: 0 18px; margin-top: 20px; } }
</style>
