#include "OrchestratorConnection.h"

#include "OrchestratorBridgeSettings.h"
#include "OrchestratorUtils.h"

#include "AssetImportTask.h"
#include "AssetToolsModule.h"
#include "IAssetTools.h"
#include "Dom/JsonObject.h"
#include "Framework/Notifications/NotificationManager.h"
#include "HAL/FileManager.h"
#include "HttpModule.h"
#include "IWebSocket.h"
#include "Interfaces/IHttpResponse.h"
#include "Misc/EngineVersion.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "WebSocketsModule.h"
#include "Widgets/Notifications/SNotificationList.h"

static const TCHAR* BridgeVersion = TEXT("1.0.0");

FOrchestratorConnection::~FOrchestratorConnection()
{
	bWanted = false;
	if (ReconnectHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(ReconnectHandle);
	}
	if (PairHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(PairHandle);
	}
	if (Socket.IsValid())
	{
		Socket->OnClosed().Clear();
		Socket->OnConnectionError().Clear();
		Socket->Close();
	}
}

static FString ServerId()
{
	FString Base = GetDefault<UOrchestratorBridgeSettings>()->ServerUrl.TrimStartAndEnd().ToLower();
	Base.RemoveFromEnd(TEXT("/"));
	return Base;
}

bool FOrchestratorConnection::HasKey() const
{
	const UOrchestratorBridgeSettings* Settings = GetDefault<UOrchestratorBridgeSettings>();
	if (Settings->DeviceKey.IsEmpty() || Settings->DeviceKeyServer != ServerId())
	{
		return false;
	}
	FDateTime Expires;
	return !FDateTime::ParseIso8601(*Settings->DeviceKeyExpires, Expires) || Expires > FDateTime::UtcNow();
}

void FOrchestratorConnection::SaveKey(const FString& Key, const FString& Expires)
{
	UOrchestratorBridgeSettings* Settings = GetMutableDefault<UOrchestratorBridgeSettings>();
	Settings->DeviceKey = Key;
	Settings->DeviceKeyExpires = Expires;
	Settings->DeviceKeyServer = Key.IsEmpty() ? FString() : ServerId();
	// Saved/Config/<Platform>Editor/EditorPerProjectUserSettings.ini -- per user, never Config/Default*.ini.
	Settings->SaveConfig();
}

void FOrchestratorConnection::Start(bool bAllowPairing)
{
	if (Socket.IsValid() || PairHandle.IsValid())
	{
		return;
	}
	if (GetDefault<UOrchestratorBridgeSettings>()->ServerUrl.TrimStartAndEnd().IsEmpty())
	{
		Status = TEXT("set Server Url in Project Settings > Plugins > Orchestrator Bridge");
		return;
	}
	if (HasKey())
	{
		bWanted = true;
		Connect();
	}
	else if (bAllowPairing)
	{
		Pair();
	}
	else
	{
		Status = TEXT("not paired -- Tools > Orchestrator Bridge > Connect / Pair");
	}
}

void FOrchestratorConnection::Stop()
{
	bWanted = false;
	if (ReconnectHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(ReconnectHandle);
		ReconnectHandle.Reset();
	}
	if (PairHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(PairHandle);
		PairHandle.Reset();
	}
	if (Socket.IsValid())
	{
		TSharedPtr<IWebSocket> Old = Socket;
		Socket.Reset();
		Old->OnClosed().Clear();
		Old->OnConnectionError().Clear();
		Old->OnMessage().Clear();
		Old->Close();
	}
	bConnected = false;
	Status = TEXT("disconnected");
}

void FOrchestratorConnection::Restart()
{
	Stop();
	Start(false);
}

void FOrchestratorConnection::Forget()
{
	Stop();
	SaveKey(FString(), FString());
	Status = TEXT("not paired -- Tools > Orchestrator Bridge > Connect / Pair");
}

FString FOrchestratorConnection::HttpUrl(const FString& Path) const
{
	FString Base = GetDefault<UOrchestratorBridgeSettings>()->ServerUrl.TrimStartAndEnd();
	Base.RemoveFromEnd(TEXT("/"));
	return Base + Path;
}

TSharedRef<IHttpRequest, ESPMode::ThreadSafe> FOrchestratorConnection::NewRequest(const FString& Verb, const FString& Path) const
{
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = FHttpModule::Get().CreateRequest();
	Req->SetVerb(Verb);
	Req->SetURL(HttpUrl(Path));
	const FString& Key = GetDefault<UOrchestratorBridgeSettings>()->DeviceKey;
	if (!Key.IsEmpty())
	{
		Req->SetHeader(TEXT("Authorization"), TEXT("Bearer ") + Key);
	}
	return Req;
}

void FOrchestratorConnection::Notify(const FString& Text, bool bError)
{
	FNotificationInfo Info(FText::FromString(Text));
	Info.ExpireDuration = bError ? 8.f : 4.f;
	Info.bUseLargeFont = false;
	TSharedPtr<SNotificationItem> Item = FSlateNotificationManager::Get().AddNotification(Info);
	if (Item.IsValid())
	{
		Item->SetCompletionState(bError ? SNotificationItem::CS_Fail : SNotificationItem::CS_Success);
	}
	if (bError)
	{
		UE_LOG(LogTemp, Warning, TEXT("[Orchestrator] %s"), *Text);
	}
	else
	{
		UE_LOG(LogTemp, Log, TEXT("[Orchestrator] %s"), *Text);
	}
}

static FString ClientName()
{
	FString Name = GetDefault<UOrchestratorBridgeSettings>()->ClientName.TrimStartAndEnd();
	return Name.IsEmpty() ? FString(FPlatformProcess::ComputerName()) : Name;
}

void FOrchestratorConnection::Pair()
{
	SaveKey(FString(), FString());
	Status = TEXT("asking the orchestrator to pair...");
	TSharedRef<FJsonObject> Body = MakeShared<FJsonObject>();
	Body->SetStringField(TEXT("app"), TEXT("unreal"));
	Body->SetStringField(TEXT("name"), ClientName());
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = NewRequest(TEXT("POST"), TEXT("/api/bridge/device/pair"));
	Req->SetHeader(TEXT("Content-Type"), TEXT("application/json"));
	Req->SetContentAsString(OrchestratorUtils::ToJson(Body));
	TWeakPtr<FOrchestratorConnection> Weak = AsShared();
	Req->OnProcessRequestComplete().BindLambda([Weak](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<FOrchestratorConnection> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		TSharedPtr<FJsonObject> Json = (bOk && Resp.IsValid()) ? OrchestratorUtils::ParseJson(Resp->GetContentAsString()) : nullptr;
		if (!Json.IsValid() || Resp->GetResponseCode() != 200)
		{
			Self->Status = FString::Printf(TEXT("pairing failed (%d) -- is the Server Url right?"), Resp.IsValid() ? Resp->GetResponseCode() : 0);
			Notify(TEXT("Orchestrator: ") + Self->Status, true);
			return;
		}
		Self->PairRequestId = Json->GetStringField(TEXT("request_id"));
		Self->PairSecret = Json->GetStringField(TEXT("secret"));
		const FString Code = Json->GetStringField(TEXT("code"));
		Self->Status = FString::Printf(TEXT("code %s -- approve it on the site: Settings > Unreal / Blender bridge"), *Code);
		FNotificationInfo Info(FText::FromString(TEXT("Orchestrator: approve this editor on the site\nSettings > Unreal / Blender bridge, code ") + Code));
		Info.ExpireDuration = 20.f;
		FSlateNotificationManager::Get().AddNotification(Info);
		Self->PairStarted = FPlatformTime::Seconds();
		Self->PairHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateLambda([Weak](float)
		{
			if (TSharedPtr<FOrchestratorConnection> S = Weak.Pin())
			{
				S->PollPairing();
				return S->PairHandle.IsValid();
			}
			return false;
		}), 2.f);
	});
	Req->ProcessRequest();
}

void FOrchestratorConnection::PollPairing()
{
	if (FPlatformTime::Seconds() - PairStarted > 11 * 60)
	{
		PairHandle.Reset();
		Status = TEXT("pairing request expired -- Connect / Pair again");
		return;
	}
	TSharedRef<FJsonObject> Body = MakeShared<FJsonObject>();
	Body->SetStringField(TEXT("secret"), PairSecret);
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req =
		NewRequest(TEXT("POST"), FString::Printf(TEXT("/api/bridge/device/pair/%s/poll"), *PairRequestId));
	Req->SetHeader(TEXT("Content-Type"), TEXT("application/json"));
	Req->SetContentAsString(OrchestratorUtils::ToJson(Body));
	TWeakPtr<FOrchestratorConnection> Weak = AsShared();
	const FString RequestId = PairRequestId;
	Req->OnProcessRequestComplete().BindLambda([Weak, RequestId](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<FOrchestratorConnection> Self = Weak.Pin();
		if (!Self.IsValid() || !Self->PairHandle.IsValid() || Self->PairRequestId != RequestId)
		{
			return;  // stopped, or an answer to an older request
		}
		TSharedPtr<FJsonObject> Json = (bOk && Resp.IsValid()) ? OrchestratorUtils::ParseJson(Resp->GetContentAsString()) : nullptr;
		if (!Json.IsValid())
		{
			return;  // network blip: the ticker asks again
		}
		const FString State = Json->GetStringField(TEXT("status"));
		if (State == TEXT("pending"))
		{
			return;
		}
		FTSTicker::GetCoreTicker().RemoveTicker(Self->PairHandle);
		Self->PairHandle.Reset();
		if (State == TEXT("approved"))
		{
			FString Expires;
			Json->TryGetStringField(TEXT("expires_at"), Expires);
			Self->SaveKey(Json->GetStringField(TEXT("key")), Expires);
			Notify(TEXT("Orchestrator: paired"));
			Self->bWanted = true;
			Self->Connect();
		}
		else
		{
			Self->Status = State == TEXT("denied") ? TEXT("pairing was denied on the site") : TEXT("pairing request expired -- Connect / Pair again");
			Notify(TEXT("Orchestrator: ") + Self->Status, true);
		}
	});
	Req->ProcessRequest();
}

void FOrchestratorConnection::Connect()
{
	const FString Key = GetDefault<UOrchestratorBridgeSettings>()->DeviceKey;
	FString Base = GetDefault<UOrchestratorBridgeSettings>()->ServerUrl.TrimStartAndEnd();
	Base.RemoveFromEnd(TEXT("/"));
	FString WsUrl;
	if (Base.StartsWith(TEXT("https://")))
	{
		WsUrl = TEXT("wss://") + Base.RightChop(8);
	}
	else
	{
		WsUrl = TEXT("ws://") + (Base.StartsWith(TEXT("http://")) ? Base.RightChop(7) : Base);
	}
	WsUrl += TEXT("/api/bridge/device/ws");

	TMap<FString, FString> Headers;
	Headers.Add(TEXT("Authorization"), TEXT("Bearer ") + Key);

	FModuleManager::LoadModuleChecked<FWebSocketsModule>(TEXT("WebSockets"));
	Socket = FWebSocketsModule::Get().CreateWebSocket(WsUrl, TEXT(""), Headers);
	Status = TEXT("connecting...");

	TWeakPtr<FOrchestratorConnection> Weak = AsShared();
	TSharedPtr<IWebSocket> This = Socket;

	Socket->OnConnected().AddLambda([Weak]()
	{
		TSharedPtr<FOrchestratorConnection> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		TSharedRef<FJsonObject> Hello = MakeShared<FJsonObject>();
		Hello->SetStringField(TEXT("type"), TEXT("hello"));
		Hello->SetStringField(TEXT("version"), FString::Printf(TEXT("%s / UE %s"), BridgeVersion, *FEngineVersion::Current().ToString(EVersionComponent::Patch)));
		Self->SendJson(Hello);
	});

	Socket->OnMessage().AddLambda([Weak](const FString& Text)
	{
		if (TSharedPtr<FOrchestratorConnection> Self = Weak.Pin())
		{
			Self->OnMessage(Text);
		}
	});

	auto Lost = [Weak, This](const FString& Why, int32 Code)
	{
		TSharedPtr<FOrchestratorConnection> Self = Weak.Pin();
		if (!Self.IsValid() || Self->Socket != This)
		{
			return;  // an old socket we already replaced
		}
		const bool bWasConnected = Self->bConnected;
		Self->bConnected = false;
		Self->Socket.Reset();
		if (Code == 4401)
		{
			// Expired or revoked: retrying can't help, the person has to approve again.
			Self->bWanted = false;
			Self->SaveKey(FString(), FString());
			Self->Status = Why + TEXT(" -- Tools > Orchestrator Bridge > Connect / Pair");
			Notify(TEXT("Orchestrator: ") + Self->Status, true);
			return;
		}
		Self->Status = TEXT("offline: ") + Why;
		if (bWasConnected)
		{
			Notify(TEXT("Orchestrator: connection lost, reconnecting"), true);
		}
		Self->ScheduleReconnect();
	};
	Socket->OnConnectionError().AddLambda([Lost](const FString& Error) { Lost(Error, 0); });
	Socket->OnClosed().AddLambda([Lost](int32 Code, const FString& Reason, bool)
	{
		Lost(Reason.IsEmpty() ? FString::Printf(TEXT("closed (%d)"), Code) : Reason, Code);
	});

	Socket->Connect();
}

void FOrchestratorConnection::ScheduleReconnect()
{
	if (!bWanted || ReconnectHandle.IsValid())
	{
		return;
	}
	TWeakPtr<FOrchestratorConnection> Weak = AsShared();
	ReconnectHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateLambda([Weak](float)
	{
		if (TSharedPtr<FOrchestratorConnection> Self = Weak.Pin())
		{
			Self->ReconnectHandle.Reset();
			if (Self->bWanted && !Self->Socket.IsValid())
			{
				Self->Connect();
			}
		}
		return false;  // once
	}), 5.f);
}

void FOrchestratorConnection::SendJson(const TSharedRef<FJsonObject>& Obj)
{
	if (Socket.IsValid() && Socket->IsConnected())
	{
		Socket->Send(OrchestratorUtils::ToJson(Obj));
	}
}

void FOrchestratorConnection::SendResult(const FString& RequestId, bool bOk, const FString& Message)
{
	TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
	Obj->SetStringField(TEXT("type"), TEXT("result"));
	Obj->SetStringField(TEXT("request_id"), RequestId);
	Obj->SetBoolField(TEXT("ok"), bOk);
	Obj->SetStringField(TEXT("message"), Message);
	SendJson(Obj);
}

void FOrchestratorConnection::OnMessage(const FString& Text)
{
	TSharedPtr<FJsonObject> Msg = OrchestratorUtils::ParseJson(Text);
	if (!Msg.IsValid())
	{
		return;
	}
	const FString Type = Msg->GetStringField(TEXT("type"));
	if (Type == TEXT("welcome"))
	{
		const bool bWasConnected = bConnected;
		bConnected = true;
		Status = TEXT("connected");
		if (!bWasConnected)
		{
			Notify(TEXT("Orchestrator: connected"));
		}
	}
	else if (Type == TEXT("import"))
	{
		HandleImport(Msg);
	}
}

void FOrchestratorConnection::HandleImport(const TSharedPtr<FJsonObject>& Msg)
{
	const FString RequestId = Msg->GetStringField(TEXT("request_id"));
	const TSharedPtr<FJsonObject>* AssetPtr = nullptr;
	if (!Msg->TryGetObjectField(TEXT("asset"), AssetPtr) || !AssetPtr)
	{
		SendResult(RequestId, false, TEXT("malformed import message"));
		return;
	}
	TSharedPtr<FJsonObject> Asset = *AssetPtr;
	const FString Format = Asset->GetStringField(TEXT("format")).ToLower();
	if (!OrchestratorUtils::CanImport(Format))
	{
		const FString Why = FString::Printf(TEXT("Unreal can't import .%s files through the bridge"), *Format);
		SendResult(RequestId, false, Why);
		Notify(TEXT("Orchestrator: ") + Why, true);
		return;
	}
	const FString AssetId = Asset->GetStringField(TEXT("id"));
	const FString LocalPath = FPaths::ConvertRelativePathToFull(
		FPaths::ProjectSavedDir() / TEXT("Orchestrator") / TEXT("Downloads") / (AssetId + TEXT(".") + Format));

	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = NewRequest(TEXT("GET"), Asset->GetStringField(TEXT("url")));
	Req->SetTimeout(900.f);
	TWeakPtr<FOrchestratorConnection> Weak = AsShared();
	Req->OnProcessRequestComplete().BindLambda([Weak, RequestId, Asset, LocalPath](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<FOrchestratorConnection> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		if (!bOk || !Resp.IsValid() || Resp->GetResponseCode() != 200)
		{
			const FString Why = FString::Printf(TEXT("download failed (%d)"), Resp.IsValid() ? Resp->GetResponseCode() : 0);
			Self->SendResult(RequestId, false, Why);
			Notify(TEXT("Orchestrator: ") + Why, true);
			return;
		}
		if (!FFileHelper::SaveArrayToFile(Resp->GetContent(), *LocalPath))
		{
			Self->SendResult(RequestId, false, TEXT("couldn't write ") + LocalPath);
			return;
		}
		Self->ImportFile(RequestId, Asset, LocalPath);
	});
	Notify(TEXT("Orchestrator: receiving ") + Asset->GetStringField(TEXT("filename")));
	Req->ProcessRequest();
}

void FOrchestratorConnection::ImportFile(const FString& RequestId, const TSharedPtr<FJsonObject>& Asset, const FString& LocalPath)
{
	const UOrchestratorBridgeSettings* Settings = GetDefault<UOrchestratorBridgeSettings>();
	FString Root = Settings->ImportRoot.TrimStartAndEnd();
	Root.RemoveFromEnd(TEXT("/"));
	if (!Root.StartsWith(TEXT("/")))
	{
		Root = TEXT("/Game/") + Root;
	}
	const FString AssetId = Asset->GetStringField(TEXT("id"));
	FString ProjectName;
	Asset->TryGetStringField(TEXT("project_name"), ProjectName);
	FString ProjectId;
	Asset->TryGetStringField(TEXT("project_id"), ProjectId);
	const FString Folder = OrchestratorUtils::SafeName(ProjectName, TEXT("Project_") + ProjectId.Left(8));
	const FString Name = OrchestratorUtils::SafeName(Asset->GetStringField(TEXT("name")), TEXT("Asset_") + AssetId.Left(8));

	UAssetImportTask* Task = NewObject<UAssetImportTask>();
	Task->AddToRoot();
	Task->Filename = LocalPath;
	Task->DestinationPath = Root / Folder;
	Task->DestinationName = Name;
	Task->bReplaceExisting = true;  // re-sending the same asset updates it in place
	Task->bAutomated = true;
	Task->bSave = true;

	FAssetToolsModule& AssetTools = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
	AssetTools.Get().ImportAssetTasks({Task});

	const TArray<FString> Imported = Task->ImportedObjectPaths;
	Task->RemoveFromRoot();
	IFileManager::Get().Delete(*LocalPath, false, true, true);

	const FString FileName = Asset->GetStringField(TEXT("filename"));
	if (Imported.Num() == 0)
	{
		const FString Why = FString::Printf(TEXT("nothing was imported from %s (is the importer for this format enabled?)"), *FileName);
		SendResult(RequestId, false, Why);
		Notify(TEXT("Orchestrator: ") + Why, true);
		return;
	}
	const FString Where = FString::Join(Imported, TEXT(", "));
	SendResult(RequestId, true, FString::Printf(TEXT("imported %s as %s"), *FileName, *Where));
	Notify(FString::Printf(TEXT("Orchestrator: imported %s"), *FileName));
}
