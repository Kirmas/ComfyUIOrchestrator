#pragma once

#include "CoreMinimal.h"
#include "Engine/DeveloperSettings.h"
#include "OrchestratorBridgeSettings.generated.h"

/**
 * Project Settings > Plugins > Orchestrator Bridge. Per user, never in source control.
 * No token to type: "Connect / Pair" asks the orchestrator, the person approves it on the
 * site's Settings page, and this editor receives its own device key (a month).
 */
UCLASS(config = EditorPerProjectUserSettings, meta = (DisplayName = "Orchestrator Bridge"))
class ORCHESTRATORBRIDGE_API UOrchestratorBridgeSettings : public UDeveloperSettings
{
	GENERATED_BODY()

public:
	virtual FName GetCategoryName() const override { return TEXT("Plugins"); }

	/** Where the orchestrator runs, e.g. http://192.168.0.3:8000 */
	UPROPERTY(config, EditAnywhere, Category = "Connection")
	FString ServerUrl = TEXT("http://192.168.0.3:8000");

	/** How this editor is listed in the orchestrator's "Send to" menu. Empty = computer name. */
	UPROPERTY(config, EditAnywhere, Category = "Connection")
	FString ClientName;

	/** Reconnect on editor start-up (only once paired -- pairing is always by hand). */
	UPROPERTY(config, EditAnywhere, Category = "Connection")
	bool bAutoConnect = true;

	/** This editor's device key, issued at approval. Not shown; cleared when it expires or is revoked. */
	UPROPERTY(config)
	FString DeviceKey;

	/** ISO time the key stops working. */
	UPROPERTY(config, VisibleAnywhere, Category = "Connection", meta = (DisplayName = "Device key valid until"))
	FString DeviceKeyExpires;

	/** Keyed to the server it was issued by: a changed Server Url means pairing again. */
	UPROPERTY(config)
	FString DeviceKeyServer;

	/** Assets sent from the orchestrator land in <ImportRoot>/<project name>/. */
	UPROPERTY(config, EditAnywhere, Category = "Import")
	FString ImportRoot = TEXT("/Game/Orchestrator");

	/** "Send to Orchestrator" places the asset as a cell on a new row by default. */
	UPROPERTY(config, EditAnywhere, Category = "Export")
	bool bPlaceAsCellByDefault = true;
};
