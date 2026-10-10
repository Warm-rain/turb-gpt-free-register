<script setup lang="ts">
useHead({ title: "系统配置 · Registrator" });
interface Field {
  key: string;
  group: string;
  label: string;
  help: string;
  type: string;
  secret?: boolean;
  chain?: boolean;
  value: any;
  choices?: { value: string; label: string }[];
}
const api = useApi(),
  toast = useToast();
const fields = ref<Field[]>([]),
  values = reactive<Record<string, any>>({}),
  saved = ref<Record<string, any>>({});
const activeGroup = ref(""),
  search = ref(""),
  loading = ref(true),
  busy = ref(false),
  error = ref("");
const chainDraft = reactive<Record<string, string>>({});
const groups = computed(() => [...new Set(fields.value.map((f) => f.group))]);
const shown = computed(() =>
  fields.value.filter((f) =>
    search.value
      ? `${f.label} ${f.key} ${f.help} ${f.group}`
          .toLowerCase()
          .includes(search.value.toLowerCase())
      : f.group === activeGroup.value,
  ),
);
const changed = computed(() =>
  fields.value.filter(
    (f) => JSON.stringify(values[f.key]) !== JSON.stringify(saved.value[f.key]),
  ),
);
function asInput(field: Field) {
  return Array.isArray(field.value)
    ? field.value.join("\n")
    : (field.value ?? (field.type === "bool" ? false : ""));
}
// ---- 多途径优先级链（注册 / 查活 / Codex 授权）----
// 配置项本身仍是字符串：链在界面上按顺序拆成若干层，保存时再拼回 "a, b, c"。
function chainItems(field: Field): string[] {
  const raw = String(values[field.key] ?? "")
    .replace(/->|=>|→|⇒|>/g, ",")
    .toLowerCase();
  return raw
    .split(/[,;|，；、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}
function chainLabel(field: Field, item: string) {
  const hit = field.choices?.find((choice) => choice.value === item);
  return hit?.label || item;
}
function chainSet(field: Field, items: string[]) {
  values[field.key] = items.join(", ");
}
function chainRemaining(field: Field) {
  const used = new Set(chainItems(field));
  return (field.choices ?? []).filter((choice) => !used.has(choice.value));
}
function chainAppend(field: Field) {
  const items = chainItems(field);
  const remaining = (field.choices ?? []).filter(
    (choice) => !items.includes(choice.value),
  );
  const draft = chainDraft[field.key];
  // 下拉里选中的途径如果已经在链里（或已被移除），退回第一个未使用的途径，
  // 保证同一个途径不会在链里出现两次。
  const next =
    draft && remaining.some((choice) => choice.value === draft)
      ? draft
      : remaining[0]?.value;
  if (!next) return;
  chainSet(field, [...items, next]);
  chainDraft[field.key] = "";
}
function chainMove(field: Field, index: number, delta: number) {
  const items = chainItems(field);
  const target = index + delta;
  if (target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
  chainSet(field, items);
}
function chainRemove(field: Field, index: number) {
  const items = chainItems(field);
  items.splice(index, 1);
  chainSet(field, items);
}
function isSecret(field: Field) {
  return (
    field.secret || /PASSWORD|SECRET|TOKEN|API_KEY|AUTH_CODE/.test(field.key)
  );
}
async function load() {
  loading.value = true;
  error.value = "";
  try {
    fields.value = await api.request<Field[]>("/api/config");
    for (const field of fields.value) values[field.key] = asInput(field);
    saved.value = { ...values };
    if (!activeGroup.value) activeGroup.value = groups.value[0] || "";
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function save() {
  busy.value = true;
  try {
    const updates: Record<string, any> = {};
    for (const f of changed.value) {
      const value = values[f.key];
      if (
        ["int", "float"].includes(f.type) &&
        (value === "" ||
          !Number.isFinite(Number(value)) ||
          (f.type === "int" && !Number.isInteger(Number(value))))
      )
        throw new Error(
          `${f.label}需要填写有效${f.type === "int" ? "整数" : "数值"}`,
        );
      updates[f.key] = ["int", "float"].includes(f.type)
        ? Number(value)
        : f.type === "list_str_multiline"
          ? String(value)
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean)
          : value;
    }
    const result = await api.request("/api/config", {
      method: "POST",
      body: { updates },
    });
    if (result.ignored?.length)
      toast.info(`以下字段未保存：${result.ignored.join("、")}`);
    toast.success(result.note || "配置已保存");
    await load();
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
const helperResult = ref(""),
  helperOpen = ref(false);
const {
  status: updateStatus,
  loading: updateLoading,
  applying: updateApplying,
  refresh: refreshUpdate,
  checkNow: checkUpdateNow,
  applyUpdate: applyUpdateNow,
} = useUpdateStatus();
const updating = ref(false);
const updateBadge = computed(() => {
  const s = updateStatus.value;
  if (s.checking || updateLoading.value) return { kind: "badge-info", label: "检查中…" };
  if (!s.checked_at) return { kind: "badge-neutral", label: "尚未检查" };
  if (s.ok === false) return { kind: "badge-danger", label: "检查失败" };
  if (s.update_available)
    return {
      kind: "badge-warning",
      label:
        typeof s.behind === "number" && s.behind > 0
          ? `有 ${s.behind} 个新提交`
          : "有新版本",
    };
  return { kind: "badge-success", label: "已是最新" };
});
async function checkUpdate() {
  try {
    await checkUpdateNow();
    const s = updateStatus.value;
    if (s.update_available) toast.info(`${updateBadge.value.label}，可在下方一键更新`);
    else if (s.ok === false) toast.error(s.error || "检查更新失败");
    else toast.success("当前已是最新版本");
  } catch (e: any) {
    toast.error(e.message);
  }
}
async function runUpdate() {
  const s = updateStatus.value;
  const source = `${s.source?.remote || "origin"}/${s.source?.branch || "main"}`;
  if (
    !window.confirm(
      `将执行 git fetch + git merge --ff-only（${source}）。\n` +
        "只允许快进合并，不会丢弃本地改动。确认更新？",
    )
  )
    return;
  updating.value = true;
  try {
    const result = await applyUpdateNow();
    toast.success(result.message || "更新完成");
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    updating.value = false;
  }
}
// ---- 上传 Key 分配：多个 Key 存数据库，明文只在生成时回显一次 ----
interface UploadKeyItem {
  id: number;
  label: string;
  note: string;
  enabled: boolean;
  use_count: number;
  last_used_at: string;
  masked: string;
  display_suffix: string;
}
const uploadKeys = ref<UploadKeyItem[]>([]),
  uploadKeysLoading = ref(true),
  uploadKeysError = ref(""),
  legacyUploadKey = ref(false),
  uploadKeyBusy = ref(false);
const uploadKeyOpen = ref(false),
  editingUploadKey = ref<UploadKeyItem | null>(null),
  uploadKeyError = ref("");
const uploadKeyForm = reactive({ label: "", note: "", enabled: true });
const issuedKey = ref(""),
  issuedLabel = ref(""),
  issuedOpen = ref(false);
async function loadUploadKeys() {
  uploadKeysLoading.value = true;
  uploadKeysError.value = "";
  try {
    const result = await api.request<{ items: UploadKeyItem[]; legacy_env_configured: boolean }>(
      "/api/upload-keys",
    );
    uploadKeys.value = result.items || [];
    legacyUploadKey.value = result.legacy_env_configured === true;
  } catch (e: any) {
    uploadKeysError.value = e.message;
  } finally {
    uploadKeysLoading.value = false;
  }
}
function openUploadKey(item: UploadKeyItem | null = null) {
  editingUploadKey.value = item;
  uploadKeyError.value = "";
  uploadKeyForm.label = item?.label || "";
  uploadKeyForm.note = item?.note || "";
  uploadKeyForm.enabled = item ? item.enabled : true;
  uploadKeyOpen.value = true;
}
function showIssuedKey(item: UploadKeyItem & { secret?: string }) {
  if (!item?.secret) return;
  issuedKey.value = item.secret;
  issuedLabel.value = item.label || "未命名 Key";
  issuedOpen.value = true;
}
async function saveUploadKey() {
  uploadKeyBusy.value = true;
  uploadKeyError.value = "";
  try {
    const body = { ...uploadKeyForm };
    const editing = editingUploadKey.value;
    const result = await api.request<{ item: UploadKeyItem & { secret?: string } }>(
      editing ? `/api/upload-keys/${editing.id}` : "/api/upload-keys",
      { method: editing ? "PUT" : "POST", body },
    );
    uploadKeyOpen.value = false;
    if (!editing) showIssuedKey(result.item);
    toast.success(editing ? "Key 已更新" : "Key 已生成");
    await loadUploadKeys();
  } catch (e: any) {
    uploadKeyError.value = e.message;
  } finally {
    uploadKeyBusy.value = false;
  }
}
async function rotateUploadKey(item: UploadKeyItem) {
  if (
    !window.confirm(
      `确定为「${item.label || item.masked}」生成新的 Key？旧 Key 会立即失效。`,
    )
  )
    return;
  uploadKeyBusy.value = true;
  try {
    const result = await api.request<{ item: UploadKeyItem & { secret?: string } }>(
      `/api/upload-keys/${item.id}/rotate`,
      { method: "POST" },
    );
    showIssuedKey(result.item);
    await loadUploadKeys();
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    uploadKeyBusy.value = false;
  }
}
async function toggleUploadKey(item: UploadKeyItem, enabled: boolean) {
  uploadKeyBusy.value = true;
  try {
    await api.request(`/api/upload-keys/${item.id}`, {
      method: "PUT",
      body: { enabled },
    });
    toast.success(enabled ? "已启用该 Key" : "已停用该 Key");
    await loadUploadKeys();
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    uploadKeyBusy.value = false;
  }
}
async function removeUploadKey(item: UploadKeyItem) {
  if (!window.confirm(`删除「${item.label || item.masked}」后，正在使用它的上传者将立即失败。继续？`))
    return;
  uploadKeyBusy.value = true;
  try {
    await api.request(`/api/upload-keys/${item.id}`, { method: "DELETE" });
    toast.success("Key 已删除");
    await loadUploadKeys();
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    uploadKeyBusy.value = false;
  }
}
async function copyIssuedKey() {
  try {
    await navigator.clipboard.writeText(issuedKey.value);
    toast.success("Key 已复制");
  } catch {
    toast.info("复制失败，请手动选择文本复制");
  }
}
function uploadKeyTime(item: UploadKeyItem) {
  return item.last_used_at ? item.last_used_at.replace("T", " ") : "尚未使用";
}
async function helper(action: string) {
  if (
    changed.value.length &&
    !window.confirm("此操作会使用当前配置并可能保存 CloudMail 配置，是否继续？")
  )
    return;
  busy.value = true;
  try {
    const result = await api.request(
      action === "roxy" ? "/api/roxy/workspaces" : `/api/cloudmail/${action}`,
      action === "roxy"
        ? {}
        : {
            method: "POST",
            body: {
              api_base: values.CLOUDMAIL_API_BASE,
              email: values.CLOUDMAIL_ADMIN_EMAIL,
              password: values.CLOUDMAIL_PASSWORD,
              token: values.CLOUDMAIL_AUTH_TOKEN,
              path: values.CLOUDMAIL_TOKEN_PATH,
            },
          },
    );
    if (action === "roxy") {
      helperResult.value = JSON.stringify(result, null, 2);
      helperOpen.value = true;
    } else {
      toast.success(result.message || "操作成功");
      await load();
    }
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
function preventLoss(event: BeforeUnloadEvent) {
  if (changed.value.length) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(() => {
  void load();
  void loadUploadKeys();
  void refreshUpdate();
  window.addEventListener("beforeunload", preventLoss);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", preventLoss));
onBeforeRouteLeave(
  () => !changed.value.length || window.confirm("还有未保存的配置，确定离开？"),
);
</script>
<template>
  <div>
    <div class="page-header">
      <div>
        <div class="eyebrow">PREFERENCES & INTEGRATIONS</div>
        <h1 class="page-title">系统配置</h1>
        <p class="page-description">
          管理注册方式、邮箱服务与第三方集成。配置按分组组织，保存后应用到新任务。
        </p>
      </div>
      <button
        class="btn btn-primary"
        :disabled="!changed.length || busy"
        @click="save"
      >
        <UiIcon name="check" />{{
          busy
            ? "正在保存…"
            : `保存更改${changed.length ? `（${changed.length}）` : ""}`
        }}
      </button>
    </div>
    <div v-if="error" class="alert alert-error" role="alert">
      {{ error }} <button class="btn btn-sm" @click="load">重新加载</button>
    </div>
    <section class="card update-card">
      <div class="card-header">
        <h2>版本与更新</h2>
        <span class="badge" :class="updateBadge.kind"
          ><span class="badge-dot" />{{ updateBadge.label }}</span
        >
      </div>
      <div class="card-body">
        <div class="update-grid">
          <div class="update-cell">
            <span class="muted">当前版本</span>
            <strong class="mono">{{
              updateStatus.local?.short || "未知版本"
            }}</strong>
            <p>{{ updateStatus.local?.subject || "未获取到本地提交信息" }}</p>
            <small class="muted"
              >{{ updateStatus.local?.branch || "-" }} ·
              {{ updateStatus.local?.date || "-" }}</small
            >
          </div>
          <div class="update-cell">
            <span class="muted">远端最新</span>
            <strong class="mono">{{
              updateStatus.remote?.short || "未检查"
            }}</strong>
            <p>{{ updateStatus.remote?.subject || "点击「立即检查」获取远端提交" }}</p>
            <small class="muted"
              >{{ updateStatus.source?.repo || "-" }}@{{
                updateStatus.source?.branch || "-"
              }}
              <template v-if="updateStatus.release?.tag">
                · 最新发布 {{ updateStatus.release.tag }}</template
              ></small
            >
          </div>
        </div>
        <p v-if="updateStatus.ok === false" class="alert alert-error" role="alert">
          {{ updateStatus.error }}
        </p>
        <details
          v-if="updateStatus.update_available && updateStatus.commits?.length"
          class="update-commits"
          open
        >
          <summary>
            待更新提交（{{ updateStatus.commits.length
            }}<template v-if="updateStatus.commits_approx">，近似列表</template>）
          </summary>
          <ul>
            <li v-for="commit in updateStatus.commits" :key="commit.sha">
              <code>{{ commit.short }}</code
              ><span>{{ commit.subject }}</span
              ><small class="muted">{{ commit.author }} · {{ commit.date }}</small>
            </li>
          </ul>
        </details>
        <p v-if="updateStatus.local?.dirty" class="muted update-note">
          工作区有 {{ updateStatus.local.dirty }} 个未提交改动，一键更新已禁用；请先提交或暂存。
        </p>        <div class="inline update-actions">
          <button class="btn" :disabled="updateLoading" @click="checkUpdate">
            <UiIcon name="refresh" />{{
              updateStatus.checking || updateLoading ? "正在检查…" : "立即检查"
            }}
          </button>
          <button
            v-if="updateStatus.update_available"
            class="btn btn-primary"
            :disabled="updating || !updateStatus.apply?.allowed"
            :title="updateStatus.apply?.allowed ? '' : updateStatus.apply?.reason"
            @click="runUpdate"
          >
            <UiIcon name="download" />{{ updating ? "正在更新…" : "一键更新" }}
          </button>
          <a
            v-if="updateStatus.source?.html_url"
            class="btn btn-ghost"
            :href="updateStatus.source.html_url"
            target="_blank"
            rel="noopener"
            ><UiIcon name="link" />在 GitHub 查看</a
          >
          <span class="muted update-meta"
            >上次检查：{{ updateStatus.checked_at_text || "尚未检查" }} · 自动检查{{
              updateStatus.enabled ? `每 ${updateStatus.interval_hours} 小时` : "已关闭"
            }}</span
          >
        </div>
      </div>
    </section>
    <section class="card upload-keys-card">
      <div class="card-header">
        <div>
          <h2>上传 Key 分配</h2>
          <p class="muted">
            公共页面 /upload 的可用 Key。可给每个上传者分配一个；停用或删除后该 Key 立即失效。
          </p>
        </div>
        <div class="inline">
          <button
            class="btn btn-sm"
            :disabled="uploadKeysLoading"
            @click="loadUploadKeys"
          >
            <UiIcon name="refresh" />刷新
          </button>
          <button class="btn btn-primary btn-sm" @click="openUploadKey()">
            <UiIcon name="plus" />新增 Key
          </button>
        </div>
      </div>
      <p v-if="uploadKeysError" class="alert alert-error" role="alert">
        {{ uploadKeysError }}
        <button class="btn btn-sm" @click="loadUploadKeys">重试</button>
      </p>
      <p v-if="legacyUploadKey" class="alert alert-warning" role="alert">
        旧版配置项 <code>PUBLIC_UPLOAD_KEY</code> 也处于生效状态；如只想使用下方分配的
        Key，请在「WebUI 授权」分组中清空它。
      </p>
      <div v-if="uploadKeys.length" class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>备注名</th>
              <th>Key</th>
              <th>状态</th>
              <th>上传次数</th>
              <th>最近使用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in uploadKeys" :key="item.id">
              <td>
                <strong>{{ item.label || "未命名" }}</strong>
                <small v-if="item.note" class="muted">{{ item.note }}</small>
              </td>
              <td class="mono">{{ item.masked }}</td>
              <td>
                <UiBadge :value="item.enabled ? 'enabled' : 'disabled'" />
              </td>
              <td>{{ item.use_count }}</td>
              <td class="muted">{{ uploadKeyTime(item) }}</td>
              <td>
                <div class="inline">
                  <button
                    class="btn btn-sm"
                    :disabled="uploadKeyBusy"
                    @click="openUploadKey(item)"
                  >
                    编辑</button
                  ><button
                    class="btn btn-sm"
                    :disabled="uploadKeyBusy"
                    @click="rotateUploadKey(item)"
                  >
                    换新</button
                  ><button
                    class="btn btn-sm"
                    :disabled="uploadKeyBusy"
                    @click="toggleUploadKey(item, !item.enabled)"
                  >
                    {{ item.enabled ? "停用" : "启用" }}</button
                  ><button
                    class="btn btn-sm btn-danger"
                    :disabled="uploadKeyBusy"
                    @click="removeUploadKey(item)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiEmpty
        v-else
        :title="uploadKeysLoading ? '正在加载…' : '尚未分配上传 Key'"
        description="新增后把 Key 发给上传者；公共上传页支持把 Key 保存在本机浏览器。"
      />
    </section>
    <div class="settings-layout">
      <aside class="card settings-nav">
        <div class="settings-search">
          <input
            v-model="search"
            class="input"
            placeholder="搜索配置项…"
            aria-label="搜索配置项"
          />
        </div>
        <nav aria-label="配置分组">
          <button
            v-for="group in groups"
            :key="group"
            :class="{ active: activeGroup === group && !search }"
            @click="
              activeGroup = group;
              search = '';
            "
          >
            {{ group
            }}<span>{{ fields.filter((f) => f.group === group).length }}</span>
          </button>
        </nav>
      </aside>
      <section class="card settings-panel">
        <div class="card-header">
          <h2>
            {{ search ? `搜索结果 · ${shown.length}` : activeGroup || "配置" }}
          </h2>
          <span class="muted">{{
            changed.length ? "有未保存的更改" : "已与服务器同步"
          }}</span>
        </div>
        <UiEmpty v-if="loading" title="正在读取配置…" description="" /><UiEmpty
          v-else-if="!shown.length"
          title="未找到配置项"
          description="试试其他名称或关键字。"
        />
        <form v-else class="settings-fields" @submit.prevent="save">
          <label v-for="field in shown" :key="field.key" class="setting-field"
            ><div>
              <strong>{{ field.label }}</strong>
              <p>{{ field.help }}</p>
              <code>{{ field.key }}</code>
            </div>
            <input
              v-if="field.type === 'bool'"
              v-model="values[field.key]"
              type="checkbox"
              :aria-label="field.label" /><div
              v-else-if="field.chain && field.choices?.length"
              class="driver-chain"
            >
              <ol class="driver-chain-list">
                <li
                  v-for="(item, index) in chainItems(field)"
                  :key="`${field.key}-${index}-${item}`"
                  class="driver-chain-item"
                >
                  <span class="driver-chain-order">{{ index + 1 }}</span>
                  <span class="driver-chain-name">{{
                    chainLabel(field, item)
                  }}</span>
                  <code class="driver-chain-id">{{ item }}</code>
                  <span class="driver-chain-actions">
                    <button
                      type="button"
                      class="btn btn-sm"
                      :disabled="index === 0"
                      :aria-label="`上移 ${chainLabel(field, item)}`"
                      @click="chainMove(field, index, -1)"
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      class="btn btn-sm"
                      :disabled="index === chainItems(field).length - 1"
                      :aria-label="`下移 ${chainLabel(field, item)}`"
                      @click="chainMove(field, index, 1)"
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      class="btn btn-sm"
                      :aria-label="`移除 ${chainLabel(field, item)}`"
                      @click="chainRemove(field, index)"
                    >
                      ✕
                    </button>
                  </span>
                </li>
                <li v-if="!chainItems(field).length" class="driver-chain-empty">
                  尚未选择途径，请从下方添加
                </li>
              </ol>
              <div v-if="chainRemaining(field).length" class="driver-chain-add">
                <select
                  v-model="chainDraft[field.key]"
                  class="select"
                  :aria-label="`${field.label}：选择要添加的途径`"
                >
                  <option
                    v-for="choice in chainRemaining(field)"
                    :key="choice.value"
                    :value="choice.value"
                  >
                    {{ choice.label }}
                  </option>
                </select>
                <button type="button" class="btn" @click="chainAppend(field)">
                  ＋ 添加为下一层
                </button>
              </div>
              <p class="driver-chain-hint">
                自上而下按优先级执行，当前层整体失败才切换到下一层。
                <input
                  v-model="values[field.key]"
                  class="input driver-chain-raw"
                  :aria-label="`${field.label}：手动编辑途径链`"
                  autocomplete="off"
                  spellcheck="false"
                />
              </p>
            </div>
            <select
              v-else-if="field.choices?.length"
              v-model="values[field.key]"
              class="select"
              :aria-label="field.label"
            >
              <option
                v-for="choice in field.choices"
                :key="choice.value"
                :value="choice.value"
              >
                {{ choice.label }}
              </option></select
            ><textarea
              v-else-if="
                field.type === 'list_str_multiline' ||
                /DOMAINS|POOL|PATTERNS/.test(field.key)
              "
              v-model="values[field.key]"
              class="textarea"
              :aria-label="field.label"
              rows="3" /><input
              v-else
              v-model="values[field.key]"
              class="input"
              :type="
                isSecret(field)
                  ? 'password'
                  : ['int', 'float'].includes(field.type)
                    ? 'number'
                    : 'text'
              "
              :step="field.type === 'float' ? 'any' : '1'"
              :aria-label="field.label"
              autocomplete="off"
          /></label>
          <div v-if="activeGroup === 'RoxyBrowser' && !search" class="inline">
            <button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('roxy')"
            >
              获取团队与项目
            </button>
          </div>
          <div v-if="activeGroup === 'CloudMail' && !search" class="inline">
            <button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('gen-token')"
            >
              生成授权 Token</button
            ><button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('domains')"
            >
              获取可用域名
            </button>
          </div>
        </form>
      </section>
    </div>
    <UiModal v-model:open="helperOpen" title="团队与项目">
      <pre class="log">{{ helperResult }}</pre>
    </UiModal>
    <UiModal
      v-model:open="uploadKeyOpen"
      :title="editingUploadKey ? '编辑上传 Key' : '新增上传 Key'"
    >
      <p v-if="uploadKeyError" class="alert alert-error" role="alert">
        {{ uploadKeyError }}
      </p>
      <form id="upload-key-form" class="stack" @submit.prevent="saveUploadKey">
        <label class="field"
          >备注名<input
            v-model="uploadKeyForm.label"
            class="input"
            maxlength="120"
            placeholder="例如 运营-张三"
          />
          <small>用于区分上传者；上传汇总通知的「上传来源」会带上这个名字。</small>
        </label>
        <label class="field"
          >备注<input
            v-model="uploadKeyForm.note"
            class="input"
            maxlength="2000"
            placeholder="可选"
        /></label>
        <label class="inline"
          ><input v-model="uploadKeyForm.enabled" type="checkbox" />启用此
          Key</label
        >
        <p v-if="!editingUploadKey" class="muted">
          保存后由服务端生成 Key，且只显示一次；请立即复制并发给上传者。
        </p>
      </form>
      <template #footer
        ><button class="btn" @click="uploadKeyOpen = false">取消</button
        ><button class="btn btn-primary" form="upload-key-form" :disabled="uploadKeyBusy">
          {{ editingUploadKey ? "保存" : "生成 Key" }}
        </button></template
      >
    </UiModal>
    <UiModal v-model:open="issuedOpen" title="新的上传 Key">
      <p>
        请立即复制并发给上传者，<strong>关闭后无法再次查看</strong>。
      </p>
      <div class="issued-key">
        <code>{{ issuedKey }}</code
        ><button class="btn btn-sm" @click="copyIssuedKey">
          <UiIcon name="check" />复制
        </button>
      </div>
      <p class="muted">{{ issuedLabel }} · 公共上传页 /upload</p>
    </UiModal>
  </div>
</template>
<style scoped>
.upload-keys-card {
  margin-bottom: 22px;
}
.upload-keys-card .card-header p {
  font-size: 11px;
  margin-top: 4px;
  max-width: 620px;
}
.upload-keys-card .data-table td:first-child small {
  display: block;
  font-size: 10px;
}
.issued-key {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 14px 0;
  padding: 12px 14px;
  border: 1px solid #cfe0ff;
  border-radius: 6px;
  background: #f4f8ff;
}
.issued-key code {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  overflow-wrap: anywhere;
  user-select: all;
}
.update-card {
  margin-bottom: 22px;
}
.update-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 18px;
}
.update-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
  border: 1px solid #eff2f5;
  border-radius: 6px;
  background: #fbfcfd;
}
.update-cell strong {
  font-size: 13px;
}
.update-cell p {
  font-size: 11px;
  line-height: 1.7;
  color: #7a8797;
  overflow-wrap: anywhere;
}
.update-cell small {
  font-size: 10px;
}
.update-commits {
  margin-top: 16px;
  font-size: 11px;
}
.update-commits summary {
  cursor: pointer;
  color: #7a8797;
}
.update-commits ul {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  max-height: 240px;
  overflow: auto;
}
.update-commits li {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: baseline;
  padding: 7px 0;
  border-bottom: 1px solid #f2f5f7;
}
.update-commits li:last-child {
  border-bottom: 0;
}
.update-commits code {
  font-size: 10px;
  color: #8592a1;
}
.update-commits span {
  overflow-wrap: anywhere;
}
.update-commits small {
  font-size: 10px;
  white-space: nowrap;
}
.update-note {
  margin-top: 14px;
  font-size: 11px;
}
.update-actions {
  margin-top: 18px;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.update-meta {
  font-size: 11px;
}
@media (max-width: 760px) {
  .update-commits li {
    grid-template-columns: 62px minmax(0, 1fr);
  }
  .update-commits small {
    grid-column: 2;
  }
}
.settings-layout {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.settings-nav {
  position: sticky;
  top: 20px;
  box-shadow: none;
}
.settings-search {
  padding: 14px;
}
.settings-nav nav {
  padding: 0 8px 12px;
  max-height: 65vh;
  overflow: auto;
}
.settings-nav button {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  background: none;
  border: 0;
  border-radius: 5px;
  padding: 10px;
  color: #7a8797;
  text-align: left;
  font-size: 12px;
}
.settings-nav button.active {
  background: var(--accent-light);
  color: var(--accent);
}
.settings-nav button span {
  font-size: 10px;
  color: #a3adb7;
}
.settings-panel {
  margin: 0 !important;
}
.settings-fields {
  padding: 0 25px 24px;
}
.setting-field {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(190px, 42%);
  align-items: center;
  gap: 30px;
  padding: 24px 0;
  border-bottom: 1px solid #eff2f5;
}
.setting-field:last-child {
  border-bottom: 0;
}
.setting-field strong {
  font-size: 12px;
  font-weight: 500;
}
.setting-field p {
  font-size: 11px;
  line-height: 1.8;
  color: #96a0ae;
  margin-top: 5px;
  overflow-wrap: anywhere;
}
.setting-field code {
  font-size: 9px;
  color: #b2bac3;
  overflow-wrap: anywhere;
}
.setting-field input[type="checkbox"] {
  justify-self: end;
  width: 18px;
  height: 18px;
}
/* 多途径优先级链：整行展示，按顺序列出每一层 */
.driver-chain {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}
.driver-chain-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
}
.driver-chain-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid #e3e8ef;
  border-radius: 6px;
  background: #fbfcfd;
}
.driver-chain-order {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #eaf1ff;
  color: #3b66f5;
  font-size: 10px;
  line-height: 20px;
  text-align: center;
}
.driver-chain-name {
  font-size: 12px;
}
.driver-chain-id {
  font-size: 9px;
  color: #b2bac3;
}
.driver-chain-actions {
  margin-left: auto;
  display: inline-flex;
  gap: 4px;
}
.driver-chain-actions .btn {
  min-width: 28px;
  padding: 2px 6px;
}
.driver-chain-empty {
  padding: 10px 12px;
  border: 1px dashed #d8dee7;
  border-radius: 6px;
  font-size: 11px;
  color: #96a0ae;
}
.driver-chain-add {
  display: flex;
  gap: 10px;
  align-items: center;
}
.driver-chain-add .select {
  flex: 1;
  min-width: 0;
}
.driver-chain-hint {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 10px;
  color: #a3adb7;
  margin: 0;
}
.driver-chain-raw {
  font-family: inherit;
}
.settings-fields > .inline {
  padding-top: 20px;
}
@media (max-width: 1000px) {
  .settings-layout {
    grid-template-columns: 170px minmax(0, 1fr);
  }
  .setting-field {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .setting-field input[type="checkbox"] {
    justify-self: start;
  }
}
@media (max-width: 760px) {
  .settings-layout {
    grid-template-columns: 1fr;
  }
  .settings-nav {
    position: static;
  }
  .settings-nav nav {
    display: flex;
    max-height: none;
    overflow: auto;
    padding: 0 10px 10px;
  }
  .settings-nav button {
    white-space: nowrap;
    gap: 15px;
    width: auto;
  }
  .settings-fields {
    padding-inline: 18px;
  }
}
</style>
