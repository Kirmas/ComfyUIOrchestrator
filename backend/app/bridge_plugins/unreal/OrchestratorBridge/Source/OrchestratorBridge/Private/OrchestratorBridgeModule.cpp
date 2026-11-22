#include "OrchestratorBridgeSettings.h"
#include "OrchestratorConnection.h"
#include "SOrchestratorSendDialog.h"

#include "ContentBrowserMenuContexts.h"
#include "ISettingsModule.h"
#include "Modules/ModuleManager.h"
#include "Styling/AppStyle.h"
#include "ToolMenus.h"

#define LOCTEXT_NAMESPACE "OrchestratorBridge"

/**
 * Wires the bridge into the editor:
 * - the connection (auto-starts if enabled, restarts when settings change);
 * - Content Browser asset right-click > "Send to Orchestrator";
 * - Tools > Orchestrator Bridge: status, reconnect, settings.
 */
class FOrchestratorBridgeModule : public IModuleInterface
{
public:
	virtual void StartupModule() override
	{
		Connection = MakeShared<FOrchestratorConnection>();
		UOrchestratorBridgeSettings* Settings = GetMutableDefault<UOrchestratorBridgeSettings>();
		SettingsHandle = Settings->OnSettingChanged().AddLambda([this](UObject*, FPropertyChangedEvent&)
		{
			Connection->Restart();
		});
		if (Settings->bAutoConnect)
		{
			Connection->Start(/*bAllowPairing=*/false);  // a pairing request is only ever made by hand
		}
		UToolMenus::RegisterStartupCallback(FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FOrchestratorBridgeModule::RegisterMenus));
	}

	virtual void ShutdownModule() override
	{
		UToolMenus::UnRegisterStartupCallback(this);
		UToolMenus::UnregisterOwner(this);
		if (UObjectInitialized())
		{
			GetMutableDefault<UOrchestratorBridgeSettings>()->OnSettingChanged().Remove(SettingsHandle);
		}
		if (Connection.IsValid())
		{
			Connection->Stop();
			Connection.Reset();
		}
	}

private:
	void RegisterMenus()
	{
		FToolMenuOwnerScoped OwnerScoped(this);

		UToolMenu* AssetMenu = UToolMenus::Get()->ExtendMenu("ContentBrowser.AssetContextMenu");
		FToolMenuSection& Section = AssetMenu->FindOrAddSection("Orchestrator");
		Section.Label = LOCTEXT("SectionLabel", "Orchestrator");
		Section.AddDynamicEntry("SendToOrchestrator", FNewToolMenuSectionDelegate::CreateLambda([this](FToolMenuSection& InSection)
		{
			const UContentBrowserAssetContextMenuContext* Context = InSection.FindContext<UContentBrowserAssetContextMenuContext>();
			if (!Context || Context->SelectedAssets.Num() == 0)
			{
				return;
			}
			const TArray<FAssetData> Selected = Context->SelectedAssets;
			TSharedPtr<FOrchestratorConnection> Conn = Connection;
			InSection.AddMenuEntry(
				"SendToOrchestrator",
				LOCTEXT("SendLabel", "Send to Orchestrator..."),
				LOCTEXT("SendTooltip", "Put the selected meshes/animations (FBX) or textures (PNG) into a project's library, optionally as a cell in one of its graphs"),
				FSlateIcon(FAppStyle::GetAppStyleSetName(), "Icons.Upload"),
				FUIAction(FExecuteAction::CreateLambda([Selected, Conn]()
				{
					SOrchestratorSendDialog::Open(Selected, Conn);
				})));
		}));

		UToolMenu* Tools = UToolMenus::Get()->ExtendMenu("LevelEditor.MainMenu.Tools");
		FToolMenuSection& ToolsSection = Tools->FindOrAddSection("Orchestrator", LOCTEXT("ToolsSection", "Orchestrator"));
		ToolsSection.AddSubMenu(
			"OrchestratorBridge",
			LOCTEXT("ToolsLabel", "Orchestrator Bridge"),
			LOCTEXT("ToolsTooltip", "Connection to ComfyUI Orchestrator"),
			FNewToolMenuDelegate::CreateLambda([this](UToolMenu* Sub)
			{
				FToolMenuSection& S = Sub->AddSection("Status");
				const FString StatusText = TEXT("Status: ") + Connection->GetStatus();
				S.AddMenuEntry("Status", FText::FromString(StatusText), FText::GetEmpty(), FSlateIcon(),
					FUIAction(FExecuteAction(), FCanExecuteAction::CreateLambda([]() { return false; })));
				TSharedPtr<FOrchestratorConnection> Conn = Connection;
				S.AddMenuEntry("Connect", LOCTEXT("Connect", "Connect / Pair"),
					LOCTEXT("ConnectTip", "Connect; if this editor isn't paired yet, ask the orchestrator -- then approve it on the site's Settings page"),
					FSlateIcon(), FUIAction(FExecuteAction::CreateLambda([Conn]() { Conn->Stop(); Conn->Start(/*bAllowPairing=*/true); })));
				S.AddMenuEntry("Disconnect", LOCTEXT("Disconnect", "Disconnect"), FText::GetEmpty(), FSlateIcon(),
					FUIAction(FExecuteAction::CreateLambda([Conn]() { Conn->Stop(); })));
				S.AddMenuEntry("Forget", LOCTEXT("Forget", "Forget pairing"),
					LOCTEXT("ForgetTip", "Delete this editor's device key; the next Connect asks for approval again"),
					FSlateIcon(), FUIAction(FExecuteAction::CreateLambda([Conn]() { Conn->Forget(); })));
				S.AddMenuEntry("Settings", LOCTEXT("Settings", "Settings..."), FText::GetEmpty(), FSlateIcon(),
					FUIAction(FExecuteAction::CreateLambda([]()
					{
						FModuleManager::LoadModuleChecked<ISettingsModule>("Settings").ShowViewer("Project", "Plugins", "OrchestratorBridgeSettings");
					})));
			}));
	}

	TSharedPtr<FOrchestratorConnection> Connection;
	FDelegateHandle SettingsHandle;
};

IMPLEMENT_MODULE(FOrchestratorBridgeModule, OrchestratorBridge)

#undef LOCTEXT_NAMESPACE
