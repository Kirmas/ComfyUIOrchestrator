#pragma once

#include "CoreMinimal.h"
#include "AssetRegistry/AssetData.h"
#include "Widgets/SCompoundWidget.h"

class FOrchestratorConnection;
class STextComboBox;
class SWindow;

/**
 * "Send to Orchestrator": pick a project and one of its graphs, choose whether
 * to also place the asset as a cell (a new row at the bottom of that graph),
 * and send. Meshes/animations go as FBX, textures as PNG; each lands in the
 * project's library (server: POST /api/bridge/device/upload).
 */
class SOrchestratorSendDialog : public SCompoundWidget
{
public:
	SLATE_BEGIN_ARGS(SOrchestratorSendDialog) {}
		SLATE_ARGUMENT(TArray<FAssetData>, Assets)
		SLATE_ARGUMENT(TSharedPtr<FOrchestratorConnection>, Connection)
		SLATE_ARGUMENT(TWeakPtr<SWindow>, Window)
	SLATE_END_ARGS()

	void Construct(const FArguments& InArgs);

	static void Open(const TArray<FAssetData>& Assets, TSharedPtr<FOrchestratorConnection> Connection);

private:
	struct FGraph
	{
		FString Id;  // empty = the project's main grid
		FString Name;
	};
	struct FProject
	{
		FString Id;
		FString Name;
		TArray<FGraph> Graphs;
	};

	void LoadTargets();
	void OnProjectChanged(TSharedPtr<FString> Item, ESelectInfo::Type);
	FReply OnSend();
	FReply OnCancel();
	void SendNext();
	void SetStatus(const FString& Text);
	bool ExportOne(UObject* Object, FString& OutPath, FString& OutMime, FString& OutError) const;

	TArray<FAssetData> Assets;
	TSharedPtr<FOrchestratorConnection> Connection;
	TWeakPtr<SWindow> Window;

	TArray<FProject> Projects;
	TArray<TSharedPtr<FString>> ProjectOptions;
	TArray<TSharedPtr<FString>> GraphOptions;
	TSharedPtr<STextComboBox> ProjectCombo;
	TSharedPtr<STextComboBox> GraphCombo;
	bool bPlace = true;
	bool bSourceGeometry = true;
	bool bBusy = false;
	FString Status;

	// sending state
	int32 NextIndex = 0;
	int32 Sent = 0;
	TArray<FString> Errors;
	FString SendProjectId;
	FString SendGraphId;
};
