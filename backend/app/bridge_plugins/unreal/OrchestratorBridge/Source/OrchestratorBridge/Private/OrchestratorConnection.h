#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "Interfaces/IHttpRequest.h"

class IWebSocket;
class FJsonObject;

/**
 * The live link to the orchestrator: a WebSocket the editor dials out on (so
 * this PC opens no port), reconnecting every few seconds while wanted. The
 * socket only carries small JSON commands; files go over plain HTTP.
 * Server side: backend/app/core/bridge.py + api/routes/bridge.py.
 *
 * Everything here runs on the game thread: IWebSocket and FHttpModule both
 * deliver their callbacks there.
 */
class FOrchestratorConnection : public TSharedFromThis<FOrchestratorConnection>
{
public:
	~FOrchestratorConnection();

	/** Connect with the stored device key; with bAllowPairing and no valid key, ask to pair first. */
	void Start(bool bAllowPairing);
	void Stop();
	/** Drop the socket and dial again (settings changed). */
	void Restart();
	/** Forget this editor's device key. */
	void Forget();
	bool HasKey() const;

	bool IsConnected() const { return bConnected; }
	const FString& GetStatus() const { return Status; }

	FString HttpUrl(const FString& Path) const;
	/** A request with the bearer token already set. */
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> NewRequest(const FString& Verb, const FString& Path) const;

	static void Notify(const FString& Text, bool bError = false);

private:
	void Connect();
	void Pair();
	void PollPairing();
	void SaveKey(const FString& Key, const FString& Expires);
	void ScheduleReconnect();
	void OnMessage(const FString& Text);
	void HandleImport(const TSharedPtr<FJsonObject>& Msg);
	void ImportFile(const FString& RequestId, const TSharedPtr<FJsonObject>& Asset, const FString& LocalPath);
	void SendJson(const TSharedRef<FJsonObject>& Obj);
	void SendResult(const FString& RequestId, bool bOk, const FString& Message);

	TSharedPtr<IWebSocket> Socket;
	FTSTicker::FDelegateHandle ReconnectHandle;
	FTSTicker::FDelegateHandle PairHandle;
	FString PairRequestId;
	FString PairSecret;
	double PairStarted = 0.0;
	bool bWanted = false;
	bool bConnected = false;
	FString Status = TEXT("disconnected");
};
