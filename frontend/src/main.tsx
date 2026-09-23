import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AnnualPrecheckPage } from "./pages/AnnualPrecheckPage";
import "./styles/app.css";

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("缺少 #root。");
}

createRoot(rootElement).render(
  <StrictMode>
    <AnnualPrecheckPage />
  </StrictMode>,
);
