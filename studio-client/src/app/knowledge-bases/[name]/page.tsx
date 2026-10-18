"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";
import { KnowledgeBaseDetailView } from "@/components/knowledge-bases/detail";

// One knowledge base, fully inspected: vitals, the YouTube source queue, standing validation.
// The name arrives as the route segment; everything else is one detail fetch.

export default function KnowledgeBaseDetailPage() {
  const params = useParams<{ name: string }>();
  const name = decodeURIComponent(params.name);
  const queryClient = useQueryClient();
  const detailKey = knowledgeBaseKeys.detail(name);

  const detail = useQuery({
    queryKey: detailKey,
    queryFn: () => knowledgeBaseApi.knowledgeBaseDetail(name),
  });

  const addSourceQueueEntry = useMutation({
    mutationFn: (url: string) => knowledgeBaseApi.addSourceQueueEntry(name, url),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: detailKey }),
  });

  const invalidateBoth = () => {
    queryClient.invalidateQueries({ queryKey: detailKey });
    queryClient.invalidateQueries({ queryKey: knowledgeBaseKeys.all });
  };

  const register = useMutation({
    mutationFn: (path: string) => knowledgeBaseApi.registerKnowledgeBase(name, path),
    onSuccess: invalidateBoth,
  });

  const unregister = useMutation({
    mutationFn: () => knowledgeBaseApi.unregisterKnowledgeBase(name),
    onSuccess: invalidateBoth,
  });

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
      <Link href="/knowledge-bases" className="text-caption text-label-3 hover:text-label-2">
        Back to Knowledge bases
      </Link>

      {detail.isPending ? <p className="text-body text-label-3">Loading {name}...</p> : null}
      {detail.isError ? (
        <p
          role="alert"
          className="rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink"
        >
          {knowledgeBaseErrorMessage(detail.error)}
        </p>
      ) : null}

      {detail.data ? (
        <KnowledgeBaseDetailView
          detail={detail.data}
          onAddUrl={(url) => addSourceQueueEntry.mutate(url)}
          adding={addSourceQueueEntry.isPending}
          addError={addSourceQueueEntry.isError ? knowledgeBaseErrorMessage(addSourceQueueEntry.error) : null}
          onRegister={(path) => register.mutate(path)}
          onUnregister={() => unregister.mutate()}
          registering={register.isPending || unregister.isPending}
          registerError={
            register.isError
              ? knowledgeBaseErrorMessage(register.error)
              : unregister.isError
                ? knowledgeBaseErrorMessage(unregister.error)
                : null
          }
        />
      ) : null}
    </div>
  );
}
