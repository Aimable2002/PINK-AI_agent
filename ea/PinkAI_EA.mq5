#property strict
#property version   "1.0.0"
#property description "PINK AI authenticated MT5 execution client"

#include <Trade\Trade.mqh>

input string InpBackendUrl = "https://your-backend.example.com";
input string InpSupabaseUrl = "https://your-project.supabase.co";
input string InpSupabaseAnonKey = "";
input string InpEmail = "";
input string InpPassword = "";
input string InpClientId = "";
input double InpVolume = 0.01;
input int InpPollSeconds = 2;
input int InpTimeoutMs = 10000;
input int InpSlippagePoints = 20;
input long InpMagicNumber = 26092301;

CTrade g_trade;
string g_access_token = "";
string g_refresh_token = "";
ulong g_token_expires_at = 0;
string g_seen_orders[];

string TrimSlash(string value)
{
   while(StringLen(value) > 0 && StringGetCharacter(value, StringLen(value) - 1) == '/')
      value = StringSubstr(value, 0, StringLen(value) - 1);
   return value;
}

string JsonEscape(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", "\\r");
   StringReplace(value, "\n", "\\n");
   return value;
}

bool HttpRequest(string method, string url, string body, string extra_headers, string &response, int &status)
{
   char request_data[];
   char response_data[];
   string response_headers;
   string headers = "Content-Type: application/json\r\n" + extra_headers;

   if (body != "")
      StringToCharArray(body, request_data, 0, WHOLE_ARRAY, CP_UTF8);

   ResetLastError();
   status = WebRequest(method, url, headers, InpTimeoutMs, request_data, response_data, response_headers);
   if (status == -1)
   {
      PrintFormat("PINK AI HTTP request failed: %s error=%d", url, GetLastError());
      response = "";
      return false;
   }

   response = CharArrayToString(response_data, 0, -1, CP_UTF8);
   return status >= 200 && status < 300;
}

string JsonString(string json, string key)
{
   string marker = "\"" + key + "\"";
   int key_pos = StringFind(json, marker);
   if (key_pos < 0)
      return "";

   int colon = StringFind(json, ":", key_pos + StringLen(marker));
   if (colon < 0)
      return "";
   int quote = StringFind(json, "\"", colon + 1);
   if (quote < 0)
      return "";

   string result = "";
   bool escaped = false;
   for (int i = quote + 1; i < StringLen(json); i++)
   {
      ushort character = StringGetCharacter(json, i);
      if (escaped)
      {
         if (character == 'n') result += "\n";
         else if (character == 'r') result += "\r";
         else result += ShortToString(character);
         escaped = false;
      }
      else if (character == '\\')
         escaped = true;
      else if (character == '"')
         return result;
      else
         result += ShortToString(character);
   }
   return "";
}

string JsonNumber(string json, string key)
{
   string marker = "\"" + key + "\"";
   int key_pos = StringFind(json, marker);
   if (key_pos < 0)
      return "";
   int colon = StringFind(json, ":", key_pos + StringLen(marker));
   if (colon < 0)
      return "";

   int start = colon + 1;
   while (start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;
   int end = start;
   while (end < StringLen(json))
   {
      ushort character = StringGetCharacter(json, end);
      if ((character >= '0' && character <= '9') || character == '-' || character == '+' || character == '.')
         end++;
      else
         break;
   }
   return StringSubstr(json, start, end - start);
}

string ExtractObject(string json, int start)
{
   int open = StringFind(json, "{", start);
   if (open < 0)
      return "";
   int depth = 0;
   bool quoted = false;
   bool escaped = false;

   for (int i = open; i < StringLen(json); i++)
   {
      ushort character = StringGetCharacter(json, i);
      if (quoted)
      {
         if (escaped) escaped = false;
         else if (character == '\\') escaped = true;
         else if (character == '"') quoted = false;
         continue;
      }
      if (character == '"') quoted = true;
      else if (character == '{') depth++;
      else if (character == '}')
      {
         depth--;
         if (depth == 0)
            return StringSubstr(json, open, i - open + 1);
      }
   }
   return "";
}

bool SeenOrder(string order_id)
{
   for (int i = 0; i < ArraySize(g_seen_orders); i++)
      if (g_seen_orders[i] == order_id)
         return true;
   return false;
}

void RememberOrder(string order_id)
{
   if (SeenOrder(order_id)) return;
   int count = ArraySize(g_seen_orders);
   ArrayResize(g_seen_orders, count + 1);
   g_seen_orders[count] = order_id;
   if (ArraySize(g_seen_orders) > 500)
   {
      for (int i = 1; i < ArraySize(g_seen_orders); i++)
         g_seen_orders[i - 1] = g_seen_orders[i];
      ArrayResize(g_seen_orders, 500);
   }
}

bool Login()
{
   string url = TrimSlash(InpSupabaseUrl) + "/auth/v1/token?grant_type=password";
   string body = "{\"email\":\"" + JsonEscape(InpEmail) + "\",\"password\":\"" + JsonEscape(InpPassword) + "\"}";
   string response;
   int status;
   if (!HttpRequest("POST", url, body, "apikey: " + InpSupabaseAnonKey + "\r\n", response, status))
   {
      PrintFormat("PINK AI Supabase login failed: HTTP %d response=%s", status, response);
      return false;
   }

   g_access_token = JsonString(response, "access_token");
   g_refresh_token = JsonString(response, "refresh_token");
   string expires = JsonNumber(response, "expires_in");
   if (g_access_token == "")
      return false;
   g_token_expires_at = GetTickCount64() + (ulong)MathMax(60, (int)StringToInteger(expires) - 60) * 1000;
   return true;
}

bool RefreshToken()
{
   if (g_refresh_token == "")
      return Login();

   string url = TrimSlash(InpSupabaseUrl) + "/auth/v1/token?grant_type=refresh_token";
   string body = "{\"refresh_token\":\"" + JsonEscape(g_refresh_token) + "\"}";
   string response;
   int status;
   if (!HttpRequest("POST", url, body, "apikey: " + InpSupabaseAnonKey + "\r\n", response, status))
   {
      PrintFormat("PINK AI token refresh failed: HTTP %d", status);
      return Login();
   }

   g_access_token = JsonString(response, "access_token");
   string refreshed = JsonString(response, "refresh_token");
   if (refreshed != "") g_refresh_token = refreshed;
   string expires = JsonNumber(response, "expires_in");
   g_token_expires_at = GetTickCount64() + (ulong)MathMax(60, (int)StringToInteger(expires) - 60) * 1000;
   return g_access_token != "";
}

bool EnsureToken()
{
   if (g_access_token == "")
      return Login();
   if (GetTickCount64() >= g_token_expires_at)
      return RefreshToken();
   return true;
}

bool BackendRequest(string method, string path, string body, string &response, int &status)
{
   if (!EnsureToken())
      return false;
   string headers = "Authorization: Bearer " + g_access_token + "\r\n";
   bool ok = HttpRequest(method, TrimSlash(InpBackendUrl) + path, body, headers, response, status);
   if (status == 401)
   {
      g_access_token = "";
      if (!RefreshToken()) return false;
      headers = "Authorization: Bearer " + g_access_token + "\r\n";
      ok = HttpRequest(method, TrimSlash(InpBackendUrl) + path, body, headers, response, status);
   }
   return ok;
}

string OrderObjectFromClaimResponse(string response)
{
   int order_key = StringFind(response, "\"order\"");
   if (order_key < 0) return "";
   return ExtractObject(response, order_key);
}

bool ClaimOrder(string order_id, string &order)
{
   string response;
   int status;
   string path = "/v1/ea/orders/" + order_id + "/claim?client_id=" + InpClientId;
   if (!BackendRequest("POST", path, "", response, status))
   {
      if (status != 409)
         PrintFormat("PINK AI claim failed for %s: HTTP %d", order_id, status);
      return false;
   }
   order = OrderObjectFromClaimResponse(response);
   return order != "";
}

bool SendReceipt(string order_id, string status_name, ulong ticket, double requested_price, double fill_price, double volume, string error_code, string error_message)
{
   string raw = "{\"retcode\":\"" + IntegerToString((int)g_trade.ResultRetcode()) + "\",\"comment\":\"" + JsonEscape(g_trade.ResultComment()) + "\"}";
   string body = "{\"mt5_account_id\":\"" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)) +
      "\",\"broker_ticket\":\"" + IntegerToString((int)ticket) +
      "\",\"status\":\"" + status_name +
      "\",\"requested_price\":" + DoubleToString(requested_price, _Digits) +
      ",\"fill_price\":" + DoubleToString(fill_price, _Digits) +
      ",\"volume\":" + DoubleToString(volume, 2) +
      ",\"error_code\":\"" + JsonEscape(error_code) +
      "\",\"error_message\":\"" + JsonEscape(error_message) +
      "\",\"raw_response\":" + raw + "}";

   string response;
   int status;
   bool ok = BackendRequest("POST", "/v1/ea/orders/" + order_id + "/execution", body, response, status);
   if (!ok)
      PrintFormat("PINK AI receipt failed for %s: HTTP %d response=%s", order_id, status, response);
   return ok;
}

bool ExecuteOrder(string order_id, string order)
{
   string symbol = JsonString(order, "symbol");
   string direction = JsonString(order, "direction");
   string order_type = JsonString(order, "order_type");
   double entry = StringToDouble(JsonNumber(order, "entry"));
   double stop_loss = StringToDouble(JsonNumber(order, "stop_loss"));
   string tp_text = JsonNumber(order, "take_profits");
   double take_profit = 0.0;
   int first_tp = StringFind(order, "\"take_profits\"");
   if (first_tp >= 0)
   {
      int bracket = StringFind(order, "[", first_tp);
      if (bracket >= 0) take_profit = StringToDouble(StringSubstr(order, bracket + 1, 32));
   }

   if (symbol == "" || (direction != "buy" && direction != "sell"))
   {
      SendReceipt(order_id, "failed", 0, entry, 0, InpVolume, "invalid_signal", "Unsupported symbol or direction");
      return false;
   }
   if (!SymbolSelect(symbol, true))
   {
      SendReceipt(order_id, "failed", 0, entry, 0, InpVolume, "symbol_unavailable", "Symbol is not available in Market Watch");
      return false;
   }

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   bool submitted = false;
   if (direction == "buy")
   {
      if (order_type == "limit") submitted = g_trade.BuyLimit(InpVolume, entry, symbol, stop_loss, take_profit, ORDER_TIME_GTC, 0, "PINK AI " + order_id);
      else if (order_type == "stop") submitted = g_trade.BuyStop(InpVolume, entry, symbol, stop_loss, take_profit, ORDER_TIME_GTC, 0, "PINK AI " + order_id);
      else submitted = g_trade.Buy(InpVolume, symbol, 0.0, stop_loss, take_profit, "PINK AI " + order_id);
   }
   else
   {
      if (order_type == "limit") submitted = g_trade.SellLimit(InpVolume, entry, symbol, stop_loss, take_profit, ORDER_TIME_GTC, 0, "PINK AI " + order_id);
      else if (order_type == "stop") submitted = g_trade.SellStop(InpVolume, entry, symbol, stop_loss, take_profit, ORDER_TIME_GTC, 0, "PINK AI " + order_id);
      else submitted = g_trade.Sell(InpVolume, symbol, 0.0, stop_loss, take_profit, "PINK AI " + order_id);
   }

   ulong ticket = g_trade.ResultOrder();
   double fill_price = g_trade.ResultPrice();
   string receipt_status = submitted ? "executed" : "rejected";
   string error_code = IntegerToString((int)g_trade.ResultRetcode());
   string error_message = g_trade.ResultComment();
   SendReceipt(order_id, receipt_status, ticket, entry, fill_price, InpVolume, error_code, error_message);
   return submitted;
}

void ProcessOrders()
{
   string response;
   int status;
   if (!BackendRequest("GET", "/v1/ea/orders?limit=50&client_id=" + InpClientId, "", response, status))
   {
      PrintFormat("PINK AI order poll failed: HTTP %d", status);
      return;
   }

   int cursor = 0;
   while (true)
   {
      int id_key = StringFind(response, "\"id\"", cursor);
      if (id_key < 0) break;
      string order_id = JsonString(StringSubstr(response, id_key), "id");
      if (order_id == "") break;
      cursor = id_key + 5;
      if (SeenOrder(order_id)) continue;

      string claimed_order;
      if (ClaimOrder(order_id, claimed_order))
      {
         RememberOrder(order_id);
         ExecuteOrder(order_id, claimed_order);
      }
   }
}

int OnInit()
{
   if (InpBackendUrl == "" || InpSupabaseUrl == "" || InpSupabaseAnonKey == "" || InpEmail == "" || InpPassword == "" || InpClientId == "")
   {
      Print("PINK AI: configure backend URL, Supabase URL/key, email, password, and client ID before starting.");
      return INIT_PARAMETERS_INCORRECT;
   }
   EventSetTimer(MathMax(1, InpPollSeconds));
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   Print("PINK AI EA started. Add both Supabase and backend URLs to MT5 WebRequest allow-list.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ProcessOrders();
}
