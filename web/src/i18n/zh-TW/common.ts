import type en from "../en/common";

export default {
  "common.appTagline": "用一個控制平面管理你自己的 GPU 和 CPU。",
  "common.loading": "載入中…",
  "common.tryAgain": "再試一次",
  "common.copy": "複製",
  "common.copied": "已複製",
  "common.cancel": "取消",
  "common.close": "關閉",
  "common.done": "完成",
  "common.open": "開啟",
  "common.advanced": "進階",
  "common.help": "說明",
  "common.trustHint":
    "這台伺服器的 HTTPS 憑證來自它自己的憑證授權單位：程式需要它的檔案 {file}，可從<link>信任這台伺服器</link>取得。下面的範例會用到它。",
  "common.role.admin": "管理員",
  "common.role.operator": "操作員",
  "common.role.user": "使用者",
  "common.status.active": "啟用中",
  "common.status.disabled": "已停用",
  "common.status.pending": "待審核",
  "common.status.revoked": "已撤銷",
  "common.status.expired": "已過期",
  "common.status.online": "線上",
  "common.status.offline": "離線",
} satisfies Record<keyof typeof en, string>;
