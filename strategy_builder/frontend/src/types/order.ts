/**
 * Order Types
 */

export type OrderAction = "BUY" | "SELL";
export type OrderType = "market" | "limit";

export interface OrderRequest {
  stock_code: string;
  stock_name: string;
  action: OrderAction;
  order_type: OrderType;
  price?: number;
  quantity: number;
  signal_reason?: string;
  market?: "domestic" | "us";
  exchange?: "NASD" | "NYSE" | "AMEX";
  confirm_prod?: boolean;
  protective_order?: {
    enabled: boolean;
    take_profit_percent?: number | null;
    stop_loss_percent?: number | null;
  };
}

export interface OrderResult {
  status: "success" | "error";
  order_id?: string;
  message: string;
}

export interface OrderConfirmData {
  stock_code: string;
  stock_name: string;
  action: OrderAction;
  order_type: OrderType;
  price: number;
  quantity: number;
  estimated_amount: number;
}
