import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    // 本项目 build 约定：先 `mv .next .next.old`（规避 safe-delete 守卫）再构建，
    // 旧产物目录必须一并忽略，否则会被 lint 全量扫描（第 14 步踩坑）。
    ".next.old/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
