#include "SOrchestratorSendDialog.h"

#include "OrchestratorBridgeSettings.h"
#include "OrchestratorConnection.h"
#include "OrchestratorUtils.h"

#include "Animation/AnimSequence.h"
#include "AssetExportTask.h"
#include "Dom/JsonObject.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/StaticMesh.h"
#include "Engine/Texture.h"
#include "Exporters/Exporter.h"
#include "Framework/Application/SlateApplication.h"
#include "HAL/FileManager.h"
#include "Interfaces/IHttpResponse.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SCheckBox.h"
#include "Widgets/Input/STextComboBox.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/SWindow.h"
#include "Widgets/Text/STextBlock.h"

#define LOCTEXT_NAMESPACE "OrchestratorBridge"

void SOrchestratorSendDialog::Open(const TArray<FAssetData>& InAssets, TSharedPtr<FOrchestratorConnection> InConnection)
{
	TSharedRef<SWindow> Win = SNew(SWindow)
		.Title(LOCTEXT("SendTitle", "Send to Orchestrator"))
		.SizingRule(ESizingRule::Autosized)
		.SupportsMaximize(false)
		.SupportsMinimize(false);
	Win->SetContent(SNew(SOrchestratorSendDialog).Assets(InAssets).Connection(InConnection).Window(Win));
	FSlateApplication::Get().AddWindow(Win);
}

void SOrchestratorSendDialog::Construct(const FArguments& InArgs)
{
	Assets = InArgs._Assets;
	Connection = InArgs._Connection;
	Window = InArgs._Window;
	bPlace = GetDefault<UOrchestratorBridgeSettings>()->bPlaceAsCellByDefault;

	FString Names;
	for (int32 i = 0; i < Assets.Num() && i < 6; ++i)
	{
		Names += (i ? TEXT(", ") : TEXT("")) + Assets[i].AssetName.ToString();
	}
	if (Assets.Num() > 6)
	{
		Names += FString::Printf(TEXT(" ... (+%d)"), Assets.Num() - 6);
	}

	ChildSlot
	[
		SNew(SBox).MinDesiredWidth(420.f).Padding(12.f)
		[
			SNew(SVerticalBox)
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 8)
			[
				SNew(STextBlock).Text(FText::FromString(Names)).AutoWrapText(true)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SNew(STextBlock).Text(LOCTEXT("Project", "Project"))
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SAssignNew(ProjectCombo, STextComboBox)
				.OptionsSource(&ProjectOptions)
				.OnSelectionChanged(this, &SOrchestratorSendDialog::OnProjectChanged)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 6, 0, 2)
			[
				SNew(STextBlock).Text(LOCTEXT("Graph", "Graph"))
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SAssignNew(GraphCombo, STextComboBox).OptionsSource(&GraphOptions)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 8)
			[
				SNew(SCheckBox)
				.IsChecked_Lambda([this]() { return bPlace ? ECheckBoxState::Checked : ECheckBoxState::Unchecked; })
				.OnCheckStateChanged_Lambda([this](ECheckBoxState S) { bPlace = S == ECheckBoxState::Checked; })
				[
					SNew(STextBlock).Text(LOCTEXT("Place", "Also place as a cell (new row in the graph)"))
				]
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 4)
			[
				SNew(STextBlock).Text_Lambda([this]() { return FText::FromString(Status); }).AutoWrapText(true)
			]
			+ SVerticalBox::Slot().AutoHeight().HAlign(HAlign_Right).Padding(0, 8, 0, 0)
			[
				SNew(SHorizontalBox)
				+ SHorizontalBox::Slot().AutoWidth().Padding(4, 0)
				[
					SNew(SButton)
					.Text(LOCTEXT("Send", "Send"))
					.IsEnabled_Lambda([this]() { return !bBusy && Projects.Num() > 0; })
					.OnClicked(this, &SOrchestratorSendDialog::OnSend)
				]
				+ SHorizontalBox::Slot().AutoWidth().Padding(4, 0)
				[
					SNew(SButton)
					.Text(LOCTEXT("Close", "Close"))
					.OnClicked(this, &SOrchestratorSendDialog::OnCancel)
				]
			]
		]
	];

	LoadTargets();
}

void SOrchestratorSendDialog::SetStatus(const FString& Text)
{
	Status = Text;
}

void SOrchestratorSendDialog::LoadTargets()
{
	bBusy = true;
	SetStatus(TEXT("Loading projects..."));
	TWeakPtr<SOrchestratorSendDialog> Weak = StaticCastSharedRef<SOrchestratorSendDialog>(AsShared());
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = Connection->NewRequest(TEXT("GET"), TEXT("/api/bridge/device/targets"));
	Req->OnProcessRequestComplete().BindLambda([Weak](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<SOrchestratorSendDialog> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		Self->bBusy = false;
		if (!bOk || !Resp.IsValid() || Resp->GetResponseCode() != 200)
		{
			const int32 Code = Resp.IsValid() ? Resp->GetResponseCode() : 0;
			Self->SetStatus(Code == 401
				? FString(TEXT("This editor isn't paired (or its key expired): Tools > Orchestrator Bridge > Connect / Pair."))
				: FString::Printf(TEXT("Couldn't reach the orchestrator (%d). Check Project Settings > Plugins > Orchestrator Bridge."), Code));
			return;
		}
		TArray<TSharedPtr<FJsonValue>> List;
		TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Resp->GetContentAsString());
		if (!FJsonSerializer::Deserialize(Reader, List))
		{
			Self->SetStatus(TEXT("Unexpected answer from the orchestrator"));
			return;
		}
		Self->Projects.Reset();
		Self->ProjectOptions.Reset();
		for (const TSharedPtr<FJsonValue>& V : List)
		{
			const TSharedPtr<FJsonObject> P = V->AsObject();
			if (!P.IsValid())
			{
				continue;
			}
			FProject Project;
			Project.Id = P->GetStringField(TEXT("id"));
			Project.Name = P->GetStringField(TEXT("name"));
			const TArray<TSharedPtr<FJsonValue>>* Graphs = nullptr;
			if (P->TryGetArrayField(TEXT("graphs"), Graphs))
			{
				for (const TSharedPtr<FJsonValue>& G : *Graphs)
				{
					const TSharedPtr<FJsonObject> GO = G->AsObject();
					if (!GO.IsValid())
					{
						continue;
					}
					FGraph Graph;
					GO->TryGetStringField(TEXT("id"), Graph.Id);  // null -> stays empty = main
					Graph.Name = GO->GetStringField(TEXT("name"));
					Project.Graphs.Add(Graph);
				}
			}
			Self->ProjectOptions.Add(MakeShared<FString>(Project.Name));
			Self->Projects.Add(MoveTemp(Project));
		}
		Self->ProjectCombo->RefreshOptions();
		if (Self->ProjectOptions.Num() > 0)
		{
			Self->ProjectCombo->SetSelectedItem(Self->ProjectOptions[0]);
			Self->SetStatus(TEXT(""));
		}
		else
		{
			Self->SetStatus(TEXT("The orchestrator has no projects yet"));
		}
	});
	Req->ProcessRequest();
}

void SOrchestratorSendDialog::OnProjectChanged(TSharedPtr<FString> Item, ESelectInfo::Type)
{
	GraphOptions.Reset();
	const int32 Index = ProjectOptions.IndexOfByKey(Item);
	if (Projects.IsValidIndex(Index))
	{
		for (const FGraph& G : Projects[Index].Graphs)
		{
			GraphOptions.Add(MakeShared<FString>(G.Name));
		}
	}
	GraphCombo->RefreshOptions();
	if (GraphOptions.Num() > 0)
	{
		GraphCombo->SetSelectedItem(GraphOptions[0]);
	}
}

FReply SOrchestratorSendDialog::OnCancel()
{
	if (TSharedPtr<SWindow> Win = Window.Pin())
	{
		Win->RequestDestroyWindow();
	}
	return FReply::Handled();
}

FReply SOrchestratorSendDialog::OnSend()
{
	const int32 P = ProjectOptions.IndexOfByKey(ProjectCombo->GetSelectedItem());
	if (!Projects.IsValidIndex(P))
	{
		SetStatus(TEXT("Pick a project"));
		return FReply::Handled();
	}
	const int32 G = GraphOptions.IndexOfByKey(GraphCombo->GetSelectedItem());
	SendProjectId = Projects[P].Id;
	SendGraphId = Projects[P].Graphs.IsValidIndex(G) ? Projects[P].Graphs[G].Id : FString();
	NextIndex = 0;
	Sent = 0;
	Errors.Reset();
	bBusy = true;
	SendNext();
	return FReply::Handled();
}

bool SOrchestratorSendDialog::ExportOne(UObject* Object, FString& OutPath, FString& OutMime, FString& OutError) const
{
	FString Ext;
	if (Object->IsA<UTexture>())
	{
		Ext = TEXT("png");
		OutMime = TEXT("image/png");
	}
	else if (Object->IsA<UStaticMesh>() || Object->IsA<USkeletalMesh>() || Object->IsA<UAnimSequence>())
	{
		Ext = TEXT("fbx");
		OutMime = TEXT("model/fbx");
	}
	else
	{
		OutError = FString::Printf(TEXT("%s: a %s can't be sent (meshes, animations and textures can)"),
			*Object->GetName(), *Object->GetClass()->GetName());
		return false;
	}
	UExporter* Exporter = UExporter::FindExporter(Object, *Ext);
	if (!Exporter)
	{
		OutError = FString::Printf(TEXT("%s: no .%s exporter for %s"), *Object->GetName(), *Ext, *Object->GetClass()->GetName());
		return false;
	}
	OutPath = FPaths::ConvertRelativePathToFull(FPaths::ProjectSavedDir() / TEXT("Orchestrator") / TEXT("Outgoing")
		/ (OrchestratorUtils::SafeName(Object->GetName(), TEXT("Asset")) + TEXT(".") + Ext));

	UAssetExportTask* Task = NewObject<UAssetExportTask>();
	Task->AddToRoot();
	Task->Object = Object;
	Task->Exporter = Exporter;
	Task->Filename = OutPath;
	Task->bSelected = false;
	Task->bReplaceIdentical = true;
	Task->bPrompt = false;
	Task->bAutomated = true;
	const bool bOk = UExporter::RunAssetExportTask(Task);
	Task->RemoveFromRoot();
	if (!bOk || !FPaths::FileExists(OutPath))
	{
		OutError = FString::Printf(TEXT("%s: export to .%s failed"), *Object->GetName(), *Ext);
		return false;
	}
	return true;
}

void SOrchestratorSendDialog::SendNext()
{
	if (NextIndex >= Assets.Num())
	{
		bBusy = false;
		FString Done = FString::Printf(TEXT("Sent %d of %d"), Sent, Assets.Num());
		if (Errors.Num())
		{
			Done += TEXT("\n") + FString::Join(Errors, TEXT("\n"));
		}
		SetStatus(Done);
		FOrchestratorConnection::Notify(TEXT("Orchestrator: ") + Done.Replace(TEXT("\n"), TEXT("; ")), Errors.Num() > 0);
		if (Errors.Num() == 0)
		{
			OnCancel();
		}
		return;
	}
	const FAssetData Data = Assets[NextIndex++];
	SetStatus(FString::Printf(TEXT("Sending %s (%d/%d)..."), *Data.AssetName.ToString(), NextIndex, Assets.Num()));

	UObject* Object = Data.GetAsset();
	FString Path, Mime, Error;
	if (!Object || !ExportOne(Object, Path, Mime, Error))
	{
		Errors.Add(Object ? Error : Data.AssetName.ToString() + TEXT(": couldn't load"));
		SendNext();
		return;
	}
	TArray<uint8> FileData;
	FFileHelper::LoadFileToArray(FileData, *Path);
	IFileManager::Get().Delete(*Path, false, true, true);

	TMap<FString, FString> Fields;
	Fields.Add(TEXT("project_id"), SendProjectId);
	Fields.Add(TEXT("dashboard_id"), SendGraphId);
	Fields.Add(TEXT("place"), bPlace ? TEXT("true") : TEXT("false"));
	Fields.Add(TEXT("source_path"), Data.GetObjectPathString());
	TArray<uint8> Body;
	const FString ContentType = OrchestratorUtils::BuildMultipart(Fields, FPaths::GetCleanFilename(Path), Mime, FileData, Body);

	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = Connection->NewRequest(TEXT("POST"), TEXT("/api/bridge/device/upload"));
	Req->SetHeader(TEXT("Content-Type"), ContentType);
	Req->SetContent(MoveTemp(Body));
	Req->SetTimeout(900.f);
	TWeakPtr<SOrchestratorSendDialog> Weak = StaticCastSharedRef<SOrchestratorSendDialog>(AsShared());
	const FString Name = Data.AssetName.ToString();
	Req->OnProcessRequestComplete().BindLambda([Weak, Name](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<SOrchestratorSendDialog> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		if (bOk && Resp.IsValid() && Resp->GetResponseCode() == 201)
		{
			Self->Sent++;
		}
		else
		{
			FString Detail = Resp.IsValid() ? Resp->GetContentAsString().Left(300) : TEXT("no answer");
			Self->Errors.Add(FString::Printf(TEXT("%s: upload failed (%d) %s"), *Name, Resp.IsValid() ? Resp->GetResponseCode() : 0, *Detail));
		}
		Self->SendNext();
	});
	Req->ProcessRequest();
}

#undef LOCTEXT_NAMESPACE
