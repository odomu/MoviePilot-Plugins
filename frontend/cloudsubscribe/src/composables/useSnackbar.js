import {ref} from "vue";

const snackbarVisible = ref(false);
const snackbarText = ref("");
const snackbarColor = ref("success");

export function isCancellationMessage(msg) {
  if (!msg) return false;
  const text = String(msg?.message || msg || "").toLowerCase();
  return (
    text.includes("cancel") ||
    text.includes("abort") ||
    text.includes("canceled") ||
    text.includes("cancelled") ||
    text.includes("http cancel") ||
    text.includes("err_canceled") ||
    text.includes("user aborted") ||
    text.includes("请求已取消") ||
    text.includes("操作已取消")
  );
}

export function useSnackbar() {
  function showMessage(msg, color = "success") {
    if (color !== "success" && isCancellationMessage(msg)) {
      return;
    }
    snackbarText.value = typeof msg === "object" && msg?.message ? msg.message : String(msg || "");
    snackbarColor.value = color;
    snackbarVisible.value = true;
  }
  return {
    snackbarVisible,
    snackbarText,
    snackbarColor,
    showMessage,
  };
}
